import argparse
import copy
import json
import os
import random
from collections import defaultdict
from pathlib import Path

from transformers import AutoTokenizer

from src import CACHE_DIR
from src.evaluation.benchmark import EvalDataLoader
from src.evaluation.prompts import ABS_SYSTEM_PROMPT, REL_SYSTEM_PROMPT
from src.evaluation.prompts import RELATIVE_PROMPT as R2R_PROMPT
from src.evaluation.utils import calculate_results, get_mode
from src.llms.prometheus_utils import batch_absolute_grade, batch_relative_grade
from src.llms.vllm_utils import VLLM

DEBUG = False
DUMMY = False


def parse_prometheus_output(outputs, mode: str):
    parts = outputs.split("[RESULT]")
    if len(parts) == 2:
        feedback, result = parts[0].strip(), parts[1].strip()
        if mode in ["a2a", "a2r"]:
            if result.isdigit() and result in ["1", "2", "3", "4", "5"]:
                return feedback, int(result)
        elif mode in ["r2r"]:
            if result in ["A", "B"]:
                return feedback, result
    return None, None


# Moddel inference
def batch_completions(
    model,
    inputs,
    params,
    mode,
    parse_output,
):
    if mode in ["a2r", "a2a"]:
        feedbacks, scores = batch_absolute_grade(model, inputs, params)
    elif mode in ["r2r"]:
        feedbacks, scores = batch_relative_grade(model, inputs, params)
    else:
        raise ValueError("Invalid mode. Must be 'a2a', 'a2r', or 'r2r'.")
    return feedbacks, scores


def collect_and_zip_feedbacks_and_scores(
    model, inputs, records, params, parse_output, batch_size=128, runs=3, mode="a2a"
):
    all_feedbacks = []
    all_scores = []

    # Execute batch_completions_with_retries multiple times and collect results
    for _ in range(runs):
        print(f"Starting run: {len(all_feedbacks) + 1}/{runs}")
        feedbacks, scores = batch_completions(model, inputs, params, mode, parse_output)

        if mode == "a2r":
            _scores = copy.deepcopy(scores)

            _accepted_scores = [_scores[i] for i in range(len(_scores)) if i % 2 == 0]
            _rejected_scores = [_scores[i] for i in range(len(_scores)) if i % 2 != 0]

            to_retry_inputs = []
            to_retry_indices = []
            assert len(_accepted_scores) == len(_rejected_scores)
            for i in range(len(_accepted_scores)):
                if _accepted_scores[i] is None or _rejected_scores[i] is None:
                    continue
                if (
                    _accepted_scores[i] == _rejected_scores[i]
                    and records[i]["tie"] != 1
                ):
                    to_retry_inputs.append(inputs[i * 2])
                    to_retry_indices.append(i * 2)
                    to_retry_inputs.append(inputs[i * 2 + 1])
                    to_retry_indices.append(i * 2 + 1)

            retries = 0
            while to_retry_inputs and retries < 2:
                retries += 1
                print(f"Retrying a2r: Attempt {retries}/2")

                _, retry_scores = batch_completions(
                    model, to_retry_inputs, params, mode, parse_output
                )

                _accepted_scores = [
                    retry_scores[i] for i in range(len(retry_scores)) if i % 2 == 0
                ]
                _rejected_scores = [
                    retry_scores[i] for i in range(len(retry_scores)) if i % 2 != 0
                ]

                new_to_retry_inputs = []
                new_to_retry_indices = []

                for i in range(len(_accepted_scores)):
                    if _accepted_scores[i] is None or _rejected_scores[i] is None:
                        continue
                    k = to_retry_indices[i * 2] // 2
                    if (
                        _accepted_scores[i] == _rejected_scores[i]
                        and records[k]["tie"] != 1
                    ):
                        new_to_retry_inputs.append(to_retry_inputs[i * 2])
                        new_to_retry_indices.append(to_retry_indices[i * 2])
                        new_to_retry_inputs.append(to_retry_inputs[i * 2 + 1])
                        new_to_retry_indices.append(to_retry_indices[i * 2 + 1])
                    else:
                        scores[to_retry_indices[i * 2]] = _accepted_scores[i]
                        scores[to_retry_indices[i * 2 + 1]] = _rejected_scores[i]

                to_retry_inputs = new_to_retry_inputs
                to_retry_indices = new_to_retry_indices

        all_feedbacks.append(feedbacks)
        all_scores.append(scores)

    # Zip feedbacks and scores
    zipped_feedbacks = list(zip(*all_feedbacks))
    zipped_scores = list(zip(*all_scores))

    # Combine feedbacks for each input across runs
    combined_feedbacks = [list(feedback_group) for feedback_group in zipped_feedbacks]
    combined_scores = [list(score_group) for score_group in zipped_scores]

    if mode == "a2r":
        accepted_feedbacks = [
            combined_feedbacks[i] for i in range(len(combined_feedbacks)) if i % 2 == 0
        ]
        rejected_feedbacks = [
            combined_feedbacks[i] for i in range(len(combined_feedbacks)) if i % 2 != 0
        ]

        accepted_scores = [
            combined_scores[i] for i in range(len(combined_scores)) if i % 2 == 0
        ]
        rejected_scores = [
            combined_scores[i] for i in range(len(combined_scores)) if i % 2 != 0
        ]

        combined_feedbacks = list(zip(accepted_feedbacks, rejected_feedbacks))
        combined_scores = list(zip(accepted_scores, rejected_scores))

    elif mode == "a2a":
        pass
    elif mode == "r2r":
        pass
    else:
        raise ValueError("Invalid mode. Must be 'a2a' or 'relative'.")

    return combined_feedbacks, combined_scores


def prepare_inputs(records, tokenizer, mode="a2a"):
    inputs = []
    # System prompt is the same for all records

    if mode == "a2a":
        system_message = ABS_SYSTEM_PROMPT
        for record in records:
            messages = [
                {"role": "user", "content": system_message + record["instruction"]},
            ]
            input_str = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs.append(input_str)
    elif mode == "a2r":
        system_message = ABS_SYSTEM_PROMPT
        for record in records:
            messages_A = [
                {
                    "role": "user",
                    "content": system_message + record["chosen_instruction"],
                },
            ]

            messages_B = [
                {
                    "role": "user",
                    "content": system_message + record["rejected_instruction"],
                },
            ]

            input_str_A = tokenizer.apply_chat_template(
                messages_A, tokenize=False, add_generation_prompt=True
            )

            input_str_B = tokenizer.apply_chat_template(
                messages_B, tokenize=False, add_generation_prompt=True
            )
            # odd index: chosen, even index: rejected
            inputs.append(input_str_A)
            inputs.append(input_str_B)
    elif mode == "r2r":
        system_message = REL_SYSTEM_PROMPT
        for record in records:
            orig_instruction = record["orig_instruction"]
            score_rubric = record["score_rubric"].split("\n")[0]
            response_A = record["orig_response_A"]
            response_B = record["orig_response_B"]

            input_str = R2R_PROMPT.format(
                orig_instruction=orig_instruction,
                response_A=response_A,
                response_B=response_B,
                score_rubric=score_rubric,
            )
            input_str = input_str.strip()
            messages = [{"role": "user", "content": system_message + input_str}]

            input_str = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )

            inputs.append(input_str)
    else:
        raise ValueError("Invalid mode. Must be 'a2a', 'a2r', or 'r2r'.")

    if False:
        random_inputs = random.sample(inputs, 3)
        for input_str in random_inputs:
            print("Random input:")
            print(input_str)
            print()

    return inputs


def main(
    model_name,
    eval_data_names: list,
    force_rerun=False,
    num_gpus=1,
    debug=False,
    strict=False,
):
    cache_dir = CACHE_DIR
    model_id = model_name.split("/")[-1]
    data_path = os.path.join(os.path.dirname(__file__), "outputs")
    report_path = os.path.join(os.path.dirname(__file__), "reports")

    assert "prometh"

    global DEBUG
    DEBUG = debug

    global STRICT
    STRICT = strict

    print("Running evaluation...")
    print(f"Debug Mode: {DEBUG}")
    print(f"Strict Mode: {STRICT}")

    print(f"Model Name: {model_name}")
    print(f"Eval Data Names: {eval_data_names}")
    print(f"Force Rerun: {force_rerun}")
    print(f"Num GPUs: {num_gpus}")
    print(f"Cache Dir: {cache_dir}")
    print(f"Data Path: {data_path}")
    print(f"Report Path: {report_path}")

    tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
    model = VLLM(model_name, num_gpus=num_gpus, cache_dir=cache_dir)
    model_mode, _ = get_mode(model_name, eval_data_names[0])

    eval_runs = []  # List of tuples of (eval_data_name, mode)
    for eval_data_name in eval_data_names:
        model_mode, data_mode = get_mode(model_name, eval_data_name)
        if model_mode == "relative" and data_mode == "relative":
            eval_runs.append((eval_data_name, "r2r", 1.0))
        elif model_mode == "absolute" and data_mode == "absolute":
            eval_runs.append((eval_data_name, "a2a", 1.0))
        elif model_mode == "absolute" and data_mode == "relative":
            eval_runs.append((eval_data_name, "a2r", 1.0))
        elif model_mode == "both" and data_mode == "relative":
            # eval_runs.append((eval_data_name, "a2r", 1.0))
            eval_runs.append((eval_data_name, "r2r", 1.0))
        elif model_mode == "both" and data_mode == "absolute":
            eval_runs.append((eval_data_name, "a2a", 1.0))

    overall_results = defaultdict(dict)

    for eval_data_name, mode, temperature in eval_runs:
        result_key = f"{eval_data_name}_{mode}_temp{temperature}"
        print(f"Running inference for {eval_data_name} in {mode} mode...")

        data_loader = EvalDataLoader(eval_data_name)
        records = data_loader.get_records()

        output_file_path = os.path.join(
            data_path,
            f"{model_id}-outputs",
            f"{result_key}_output.json",
        )

        output_path = Path(output_file_path)

        if output_path.exists() and not force_rerun:
            print("Output file already exists. Skipping inference.")
            sub_results = calculate_results(output_file_path, mode=mode)
            print(sub_results)
            overall_results[result_key] = sub_results
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)

        inputs = prepare_inputs(records, tokenizer, mode=mode)
        parse_output = parse_prometheus_output

        # Section for counting tokens
        # num_tokens_per_input = []
        # tokens_exceeding_max_length = []

        # for index, input_text in enumerate(inputs):
        #     tokenized_input = tokenizer(input_text, return_tensors="pt")
        #     num_tokens = len(tokenized_input['input_ids'][0])
        #     num_tokens_per_input.append(num_tokens)
        #     if num_tokens > 4096:
        #         tokens_exceeding_max_length.append(num_tokens)

        # if tokens_exceeding_max_length:
        #     import pdb; pdb.set_trace()

        # print(f"Number of tokens exceeding max length: {len(tokens_exceeding_max_length)}")
        # continue

        assert parse_output is not None

        params = {
            "max_tokens": 1024 if mode == "a2a" else 1024,
            "repetition_penalty": 1.03,
            "best_of": 1,
            "temperature": temperature,
            "top_p": 0.9,
        }

        feedbacks, scores = collect_and_zip_feedbacks_and_scores(
            model,
            inputs,
            records,
            params,
            parse_output,
            batch_size=1024,
            # batch_size=1, # [DEBUG] Use batch_size=1 when debugging
            runs=1 if mode != "a2a" else 3,
            mode=mode,
        )

        with output_path.open("w") as file:
            for i, record in enumerate(records):
                record["prometheus_output"] = feedbacks[i]
                record["prometheus_score"] = scores[i]
                file.write(json.dumps(record) + "\n")

        sub_results = calculate_results(output_file_path, mode=mode)
        print(sub_results)
        overall_results[result_key] = sub_results

    def format_results(results):
        for eval_name, eval_data in results.items():
            print(f"{eval_name}:")
            for category, values in eval_data.items():
                if isinstance(values, float):
                    # Format averages with .4f
                    print(f"  {category}: {values*100:.2f}")
                else:
                    print(f"  {category}:")
                    for metric, value in values.items():
                        # Format correlation values with .3f
                        # import pdb; pdb.set_trace()
                        if isinstance(value, float):
                            print(f"    {metric}: {value:.3f}")
                        else:
                            print(f"    {metric}: {value}")

    format_results(overall_results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run model evaluation.")
    parser.add_argument(
        "--model_name",
        type=str,
        default="kaist-ai/prometheus-7b-v2.0",
        help="Name of the model to evaluate",
    )
    parser.add_argument(
        "--eval_data_names",
        nargs="+",  # This allows multiple eval data names to be provided
        default=[
            "hhh_alignment_eval",
            "vicuna_eval",
            "flask_eval",
            "mt_bench_eval",
            "mt_bench_human_judgement_eval",
            "autoj_pairwise",
            "feedback_collection_ood_test",
            "preference_collection_ood_test",
        ],
        help="List of evaluation data names",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Parse outputs strictly",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="Use system prompt during evaluation",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Use system prompt during evaluation",
    )

    # You can add more arguments here if needed
    args = parser.parse_args()

    if "8x7b" in args.model_name:
        num_gpus = 4
    else:
        num_gpus = 1

    main(
        args.model_name,
        args.eval_data_names,
        force_rerun=False,
        num_gpus=num_gpus,
        debug=args.debug,
        strict=args.strict,
    )
