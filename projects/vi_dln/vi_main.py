import datetime
import json
import logging
import os
from collections import Counter

import click
import numpy as np
import tqdm
from termcolor import colored
from torch.utils.tensorboard import SummaryWriter

from dln.dataset import init_dataset
from dln.loss import LossRegistry
from dln.operator import LLMRegistry, isolated_cost
from dln.postprocessing import postprocess_prediction
from dln.score import LogProbsScore
from dln.vi.model import VILModel, log_message
from dln.vi.sampler import PosteriorSampler, PromptSampler
from dln.vi.utils import ResultLogWriter

try:
    import wandb
    wandb_installed = True
except ImportError:
    wandb_installed = False


def init_prompts(dataset, init_p1, init_p2):
    """Initialize the prompts for the two layers of the model.
    If init_p1 or init_p2 is a json file, load the best weights from the json file.
    """

    if init_p1 and init_p1.endswith(".json"):
        with open(init_p1) as f:
            best_weights = json.load(f)
        init_p1 = best_weights[dataset.dataset_name]["best_weights"]
    elif init_p2 and init_p2.endswith(".json"):
        with open(init_p2) as f:
            best_weights = json.load(f)
        init_p2 = best_weights[dataset.dataset_name]["best_weights"]
    elif init_p2 and init_p2.endswith(".log"):
        found = False
        with open(init_p2) as f:
            lines = f.readlines()
            for line in lines:
                if "Best L2 weights" in line:
                    init_p2 = line.partition("Best L2 weights:")[-1].strip()
                    found = True
                    break
            if not found:
                raise ValueError("Best weights were not found in the log file!")

    if init_p2 is None:
        init_p2 = dataset.instruction

    if init_p1 is None:
        init_p1 = ""

    return init_p1, init_p2


def validate(dataset, model, loss_fn, iteration, val_scores, writer, result_writer):
    log_message(colored("VALIDATING...", "red"))
    log_message("Current L1 weights:", model.encoder_l1.weight)
    log_message("Current L2 weights:", model.encoder_l2.weight)

    val_key = "{}-{}".format(model.encoder_l1.weight, model.encoder_l2.weight)
    if val_key in val_scores:
        log_message("Already evaluated this configuration, skipping...")
        dev_acc = val_scores[val_key]
    else:
        acc = 0.0
        tot = 0.0
        pbar = tqdm.tqdm(
            total=dataset.dev_size,
            bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}",
            desc="Eval",
        )
        dataset.reset_pointer("dev")
        class_counter = Counter()
        total_counter = Counter()

        for batch in dataset.iterate("dev", batch_size=20):
            x, y, infos = batch
            y_hat = model.forward(np.array(x), infos=infos)
            result_writer.write_examples(iteration, x, y, model.result_entry.outputs, model.result_entry.hiddens)
            losses = loss_fn(y_hat, y)
            acc += len(y) - np.sum(losses)
            tot += len(y)

            for xi, yi, yhati, li in zip(x, y, y_hat, losses):
                total_counter.update([yi])
                if li > 0:
                    class_counter.update([yi])

            pbar.update(len(y))
            pbar.set_postfix_str(f"{acc / tot:.1%}")

        for k, v in class_counter.items():
            log_message(f"{k}: {float(v)/total_counter[k]}")
        dev_acc = acc / tot
        val_scores[val_key] = dev_acc

    if iteration == 0:
        log_message(colored("INIT DEV ACC: {}".format(dev_acc), "red"))
    log_message(colored("DEV ACC: {}".format(dev_acc), "red"))
    writer.add_scalar("dev/acc", (dev_acc), iteration)
    return dev_acc


def test(dataset, model, loss_fn, iteration, writer, cost_only=False):
    log_message(colored("TESTING...", "red"))
    acc = 0.0
    tot = 0.0
    all_accs = []

    pbar = tqdm.tqdm(
        total=dataset.test_size,
        bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}",
        desc="Eval",
    )
    with isolated_cost(model.forward_evaluate, add_cost_to_total=True):
        dataset.reset_pointer("test")
        for batch in dataset.iterate("test", batch_size=20):
            x, y, infos = batch
            y_hat = model.forward(np.array(x), infos=infos, cost_only=cost_only)
            all_accs += (1. - loss_fn(y_hat, y)).tolist()
            acc += len(y) - np.sum(loss_fn(y_hat, y))
            tot += len(y)
            pbar.update(len(y))
            pbar.set_postfix_str(f"{acc / tot:.1%}")
        test_cost = model.forward_evaluate.total_cost

    test_acc = acc / tot
    if iteration == 0:
        log_message(colored("INIT TEST ACC: {}".format(test_acc), "red"))

    log_message(colored("TEST ACC: {}".format(test_acc), "red"))
    writer.add_scalar("test/acc", (test_acc), iteration)
    # for sig-test purposes
    log_message("ALL ACCS:", all_accs)
    log_message("TEST TOKEN COST:", test_cost)
    return test_acc


@click.command()
@click.option("--seed", default=42, help="Random seed.")
@click.option("--out_dir", default="log/")
@click.option("--data_dir", default="../../data")
@click.option("--max_train_size", default=-1, type=int, help="Use only so many train examples.")
@click.option("--max_dev_size", default=-1, type=int, help="Use only so many dev examples.")
@click.option("--max_test_size", default=-1, type=int, help="Use only so many test examples.")
@click.option("--val_freq", default=2)
@click.option("--do_first_eval", is_flag=True)
@click.option("--do_zero_shot", is_flag=True)
@click.option("--n_shots", default=-1, type=int)
@click.option("--q_hidden", default="suffix_forward_tbs")
@click.option("--q_prompt", default="q_action_prompt")
@click.option("--p_hidden", default="suffix_forward_tbs")
@click.option("--p_class", default="classify_forward")
@click.option("--p_residual", type=str, default="classify_residual")
@click.option("--balance_batch", is_flag=True, help="Balance batch.")
@click.option("--batch_size", type=int, default=20)
@click.option("--one_layer", type=bool, default=False)
@click.option("--dataset", type=str, default="subj")
@click.option("--use_h_argmax", type=bool, default=False)
@click.option("--iters", type=int, default=20)
@click.option("--num_p_samples", type=int, default=5)
@click.option("--num_h_samples", type=int, default=3)
@click.option("--tolerance", type=int, default=-1)
@click.option("--cost_only", is_flag=True)
@click.option(
    "--strip_options_for_hidden",
    type=bool,
    default=False,
    help="Remove options from examples for the hidden layer.",
)
@click.option(
    "--trust_factor",
    default=0.0,
    help="Trust-region factor for prompt update. Ensures KL divergence between the old and new prompt is small.",
)
@click.option(
    "--fwd_temp",
    default=0.0,
    help="Forward temperature",
)
@click.option(
    "--bwd_temp",
    default=0.7,
    help="Backward temperature",
)
@click.option(
    "--use_memory",
    type=int,
    default=0,
    help="Include evaluation of past prompts that have worked well in the selection list.",
)
@click.option(
    "--forward_use_classes",
    type=bool,
    default=True,
    help="Uses classes in the forward pass, constrains the output space.",
)
@click.option(
    "--init_p1",
    type=str,
    default=None,
)
@click.option(
    "--init_p2",
    type=str,
    default=None,
)
@click.option(
    "--held_out_prompt_ranking",
    type=bool,
    default=False,
    help="Evaluate prompts to keep for the next iteration on held-out examples in the current batch.",
)
@click.option(
    "--train_p1",
    type=bool,
    default=True,
    help="Train 1 layer, if False, keep it fixed.",
)
@click.option(
    "--train_p2",
    type=bool,
    default=True,
    help="Train 2 layer, if False, keep it fixed.",
)
@click.option(
    "--logp_penalty",
    type=float,
    default=0.0,
    help="Logp penalty for hiddens that haven't worked. Encourages exploration.",
)
@click.option(
    "--decay_logp_penalty",
    type=bool,
    default=True,
    help="Decay logp penalty linearly, reaching zero at the last iteration.",
)
@click.option(
    "--output_scoring_function",
    type=str,
    default="logprobs",
    help="Use logprobs to score output predictions.",
)
@click.option(
    "--hidden_scoring_function",
    type=str,
    default="logprobs",
    help="Use logprobs to score hidden states",
)
@click.option(
    "--loss_function",
    type=str,
    default="exact_match_loss",
    help=f"Loss function. One of {LossRegistry.available_losses()}",
)
@click.option(
    "--posterior_sharpening_include_prior",
    type=bool,
    default=True,
    help="Include prior term in the posterior sharpening.",
)
@click.option(
    "--posterior_sharpening_use_mi_regularization",
    type=bool,
    default=False,
    help="MI-type regularization term on the hidden states.",
)
@click.option(
    "--posterior_temp",
    type=float,
    default=1.0,
    help="Sharpen (<1.0)/Flatten (>1.0) the posterior distribution over h.",
)
@click.option(
    "--fwd_model_type",
    type=str,
    default="text-davinci-003",
    help="Model type for forward.",
)
@click.option(
    "--bwd_model_type",
    type=str,
    default="",
    help="Model type for backward. If not specified, use the same as fwd_model_type.",
)
@click.option(
    "--fwd_max_tokens",
    type=int,
    default=256,
    help="Forward max tokens.",
)
@click.option(
    "--bwd_max_tokens",
    type=int,
    default=512,
    help="Backward max tokens.",
)
@click.option(
    "--p1_max_tokens",
    type=int,
    default=256,
    help="P1 max tokens.",
)
@click.option(
    "--p2_max_tokens",
    type=int,
    default=20,
    help="P2 max tokens.",
)
@click.option(
    "--num_p1_steps",
    type=int,
    default=1,
    help="Number of prompt optimization steps for the hidden layer.",
)
@click.option(
    "--use_nce",
    type=bool,
    default=False,
    help="Use NCE for hidden scoring.",
)
@click.option(
    "--result_data_path",
    type=str,
    default=None,
    help="The path of the file where the result logs json are stored",
)
@click.option(
    "--enable_wandb",
    is_flag=True,
    help="Enable wandb logging. Requires wandb to be installed.",
)
def main(
    seed,
    out_dir,
    data_dir,
    max_train_size,
    max_dev_size,
    max_test_size,
    val_freq,
    cost_only,
    do_first_eval,
    do_zero_shot,
    n_shots,
    q_hidden,
    q_prompt,
    p_hidden,
    p_class,
    p_residual,
    fwd_temp,
    bwd_temp,
    balance_batch,
    batch_size,
    one_layer,
    dataset,
    use_h_argmax,
    iters,
    num_p_samples,
    num_h_samples,
    strip_options_for_hidden,
    trust_factor,
    use_memory,
    init_p1,
    init_p2,
    tolerance,
    output_scoring_function,
    hidden_scoring_function,
    loss_function,
    posterior_sharpening_include_prior,
    posterior_sharpening_use_mi_regularization,
    forward_use_classes,
    held_out_prompt_ranking,
    train_p1,
    train_p2,
    logp_penalty,
    decay_logp_penalty,
    posterior_temp,
    fwd_model_type,
    bwd_model_type,
    fwd_max_tokens,
    bwd_max_tokens,
    p1_max_tokens,
    p2_max_tokens,
    num_p1_steps,
    use_nce,
    result_data_path,
    enable_wandb,
):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")
    out_dir = os.path.join(out_dir, timestamp)
    os.makedirs(out_dir, exist_ok=True)
    output_log_dir = os.path.join(out_dir, "output.log")
    logging.basicConfig(
        filename=output_log_dir,
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_message(json.dumps(locals()))
    log_message(f"Logging to... {output_log_dir}")

    wandb_enabled = False
    if enable_wandb:
        if wandb_installed:
            wandb_enabled = True
            wandb.init(config=locals(), project="dln")
            prompt_table = wandb.Table(columns=["epoch", "w1", "w2"])
        else:
            log_message(colored("Wandb is not installed. Please install it to enable wandb logging.", "red"))

    writer = SummaryWriter(out_dir)

    dataset = init_dataset(
        dataset_id=dataset,
        seed=seed,
        data_dir=data_dir,
        n_few_shots=n_shots,
        max_train_size=max_train_size,
        max_dev_size=max_dev_size,
        max_test_size=max_test_size,
    )

    if result_data_path is None:
        result_data_path = os.path.join(out_dir, "result_data.json")
    result_writer = ResultLogWriter(dataset.dataset_name, path=result_data_path)

    init_p1, init_p2 = init_prompts(dataset, init_p1, init_p2)
    if wandb_enabled:
        prompt_table.add_data(0, init_p1, init_p2)
    log_message("Init P1: ", init_p1)
    log_message("Init P2: ", init_p2)

    # Use the same model type if bwd is not specified.
    bwd_model_type = bwd_model_type or fwd_model_type

    llm_registry = LLMRegistry()

    fwd_model = llm_registry.register(
        "fwd_model",
        fwd_model_type,
        temperature=0.0,
        max_tokens=fwd_max_tokens,
        stop=None,
    )

    bwd_model = llm_registry.register(
        "bwd_model",
        bwd_model_type,
        temperature=bwd_temp,
        max_tokens=bwd_max_tokens,
        stop=None,
    )

    postproc = None
    if loss_function == "exact_match_loss":
        postproc = postprocess_prediction
    loss_fn = LossRegistry.instantiate(loss_function, postproc)
    prompt_sampler = PromptSampler(bwd_model, q_prompt)
    posterior_sampler = PosteriorSampler(bwd_model, q_hidden)
    logprobs_score = LogProbsScore(fwd_model)
    model = VILModel(
        loss_fn,
        init_p1=init_p1,
        init_p2=init_p2,
        two_layers=not one_layer,
        num_p_samples=num_p_samples,
        num_h_samples=num_h_samples,
        forward_evaluate=fwd_model,
        posterior_sampler=posterior_sampler,
        prompt_sampler_1=prompt_sampler,
        prompt_sampler_2=prompt_sampler,
        logprobs_score=logprobs_score,
        p_hidden=p_hidden,
        p_class=p_class,
        p_residual=p_residual,
        use_h_argmax=use_h_argmax,
        output_classes=dataset.output_classes,
        strip_options_for_hidden=strip_options_for_hidden,
        trust_factor=trust_factor,
        forward_use_classes=forward_use_classes,
        held_out_prompt_ranking=held_out_prompt_ranking,
        use_memory=use_memory,
        train_p1=train_p1,
        train_p2=train_p2,
        logp_penalty=logp_penalty,
        p1_max_tokens=p1_max_tokens,
        p2_max_tokens=p2_max_tokens,
        posterior_temp=posterior_temp,
        output_scoring_function=output_scoring_function,
        hidden_scoring_function=hidden_scoring_function,
        num_p1_steps=num_p1_steps,
        posterior_sharpening_include_prior=posterior_sharpening_include_prior,
        posterior_sharpening_use_mi_regularization=posterior_sharpening_use_mi_regularization,
        use_nce=use_nce,
    )

    running_acc = 0.0
    running_elbo = 0.0
    best_dev = 0.0
    best_ps = [model.encoder_l1.weight, model.encoder_l2.weight]
    val_scores = {}

    patience = 0
    for iteration in range(iters + 1):
        log_message("STARTING EPOCH {} - {}".format(iteration, out_dir))

        if (iteration == 0 and do_first_eval) or (iteration > 0 and iteration % val_freq == 0):
            dev_acc = validate(
                dataset, model, loss_fn, iteration, val_scores, writer, result_writer
            )
            if wandb_enabled:
                wandb.log({"dev/acc": dev_acc, "epoch": iteration})

            model.result_entry.log_metric('dev_acc', dev_acc)

            if dev_acc > best_dev:
                best_dev = dev_acc
                best_ps = (model.encoder_l1.weight, model.encoder_l2.weight)

                if use_memory:
                    model.add_to_memory(*best_ps, score=best_dev)

                log_message(colored("BEST DEV ACC: {}".format(best_dev), "red"))
                patience = 0
            else:
                patience += 1

            if tolerance >= 0 and patience >= tolerance:
                log_message("Loading back the best model...")
                model.encoder_l1.weight = best_ps[0]
                model.encoder_l2.weight = best_ps[1]
                patience = 0
        else:
            model.result_entry.log_metric('dev_acc', None)

        result_writer.write_result(
            step=iteration,
            layers=[model.encoder_l2.weight] if one_layer else [model.encoder_l1.weight, model.encoder_l2.weight],
            metrics=model.result_entry.metrics,
            candidates=model.result_entry.candidates,
        )

        # zero shot or allow last iteration for validation
        if do_zero_shot or iteration == iters or cost_only or (n_shots >= 0 and not train_p1 and not train_p2):
            break

        x, y, infos = dataset.get_batch(
            "train", batch_size, random_sample=True, balance=balance_batch
        )

        if decay_logp_penalty:
            model.logp_penalty = logp_penalty * (1.0 - (iteration / iters))

        log_message(colored("Training P2? {}".format(model.train_p2), "red"))
        log_message(colored("LOGPenalty? {}".format(model.logp_penalty), "red"))
        elbo, p1, p2, loss, elbo1, elbo2 = model.forward(
            np.array(x), np.array(y), infos=infos, temperature=fwd_temp
        )

        # Update prompts
        model.encoder_l1.weight = p1
        model.encoder_l2.weight = p2
        log_message("Current L1 weights:", model.encoder_l1.weight)
        log_message("Current L2 weights:", model.encoder_l2.weight)
        log_message("Patience: {}".format(patience))

        if iteration == 0:
            running_elbo = elbo
            running_acc = 1.0 - loss
        else:
            running_elbo = 0.2 * elbo + 0.8 * running_elbo
            running_acc = 0.2 * (1.0 - loss) + 0.8 * running_acc

        log_message("--------------------")
        log_message(colored("{} TRAINING EPOCH DONE.".format(iteration), "blue"))
        log_message(colored("ELBO: {}".format(elbo), "blue"))
        log_message(colored("ACC: {}".format((1.0 - loss)), "blue"))
        log_message(colored("RUN ELBO: {}".format(running_elbo), "blue"))
        log_message(colored("RUN ACC: {}".format(running_acc), "blue"))
        log_message(colored("BATCH Y BALANCE: {}".format(Counter(y)), "blue"))
        log_message(colored("BATCH X LEN: {}".format([len(x_i) for x_i in x]), "blue"))

        if wandb_enabled:
            prompt_table.add_data(iteration + 1, str(p1), str(p2))
            wandb.log({"train/prompts" : prompt_table})
            wandb.log({"train/elbo": elbo, "train/acc": (1.0 - loss), "epoch": iteration})

        writer.add_scalar("elbo", elbo, iteration)
        writer.add_scalar("elbo1", elbo1, iteration)
        writer.add_scalar("elbo2", elbo2, iteration)
        writer.add_scalar("acc", (1.0 - loss), iteration)
        model.result_entry.log_metric('elbo', elbo)
        model.result_entry.log_metric('acc', (1.0 - loss))
        model.result_entry.log_metric('run_elbo', running_elbo)
        model.result_entry.log_metric('run_acc', running_acc)

    log_message("--------------------")
    log_message("Loading best model...")

    model.encoder_l1.weight = best_ps[0]
    model.encoder_l2.weight = best_ps[1]

    log_message("Best L1 weights:", model.encoder_l1.weight)
    log_message("Best L2 weights:", model.encoder_l2.weight)

    log_message("TRAINING TOKEN COST:", llm_registry.total_cost)
    test_acc = test(dataset, model, loss_fn, iteration, writer, cost_only=cost_only)

    if wandb_enabled:
        wandb.log({"test/acc": test_acc, "epoch": iteration})

    log_message(colored("DEV ACC: {}".format(best_dev), "green"))
    log_message(colored("TEST ACC: {}".format(test_acc), "green"))
    log_message("TOTAL TOKEN COST:", llm_registry.total_cost)

    result_writer.save_to_json_file()
    writer.close()


if __name__ == "__main__":
    main()
