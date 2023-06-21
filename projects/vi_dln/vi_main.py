import json
from collections import Counter

import datetime
import logging
import os

import click
import numpy as np
import tqdm
from dln.dataset import Dataset
from dln.loss import ZeroOneLoss
from dln.operator import backward_instantiate, forward_instantiate
from dln.score import OutputClasses
from dln.postprocessing import postprocess_prediction
from termcolor import colored
from torch.utils.tensorboard import SummaryWriter

from dln.vi.model import VILModel, log_message


def init_prompts(dataset, init_p1, init_p2):
    """ Initialize the prompts for the two layers of the model.
    If init_p1 or init_p2 is a json file, load the best weights from the json file.
    """

    if init_p1 and init_p1.endswith('.json'):
        with open(init_p1) as f:
            best_weights = json.load(f)
        init_p1 = best_weights[dataset]["best_weights"]
    elif init_p2 and init_p2.endswith('.json'):
        with open(init_p2) as f:
            best_weights = json.load(f)
        init_p2 = best_weights[dataset]["best_weights"]
    return init_p1, init_p2


def init_dataset(dataset, seed):
    val_examples = -1
    if dataset == "subj":
        prefix = ""
        task_description = (
            "Read the following sentence, then choose whether it is subjective or objective."
        )
        dataset = Dataset("../../data/ordered_prompt", "subj", seed)
        output_classes = OutputClasses(protos=["subjective", "objective"])
    elif dataset == "trec":
        prefix = ""
        task_description = "Read the following question, then choose whether it is about a description, entity, expression, human, location or number."
        dataset = Dataset("../../data/ordered_prompt", "trec", seed)
        output_classes = OutputClasses(
            protos=[
                "description",
                "entity",
                "expression",
                "human",
                "location",
                "number",
            ]
        )
    elif dataset == "mpqa":
        prefix = ""
        task_description = "Read the following review, then choose whether it is negative or positive."
        dataset = Dataset("../../data/ordered_prompt", "mpqa", seed)
        output_classes = OutputClasses(
            protos=["negative", "positive"]
        )
    elif dataset == "disaster":
        prefix = ""
        task_description = "Read the following sentence, then choose whether it is relevant to a disaster."
        dataset = Dataset("../../data/leopard", "disaster", seed)
        output_classes = OutputClasses(
            protos=["no", "yes"]
        )
    elif dataset == "airline":
        prefix = ""
        task_description = "Read the following sentence, then choose whether it is positive, negative, or neutral."
        dataset = Dataset("../../data/leopard", "airline", seed, append_options=True)
        output_classes = OutputClasses(
            protos=["positive", "negative", "neutral"]
        )
    elif dataset == "hyperbaton":
        prefix = "Which sentence has the correct adjective order:\n"
        task_description = prefix.strip()
        dataset = Dataset(
            "../../data/bbh",
            "hyperbaton",
            seed,
        )
        val_examples = 300
        output_classes = OutputClasses(protos=["a|A", "b|B"])
    elif dataset == "navigate":
        # do not strip the instruction
        prefix = (
            "If you follow these instructions, do you return to the starting point?"
        )
        task_description = prefix.strip()
        dataset = Dataset(
            "../../data/bbh", "navigate", seed,
        )
        output_classes = OutputClasses(protos=["yes|Yes", "no|No"])
    elif dataset == "date_understanding":
        # do not strip the instruction
        prefix = "Infer the date from context."
        task_description = prefix.strip()
        dataset = Dataset("../../data/bbh", "date_understanding", seed)
        output_classes = OutputClasses(
            protos=["a|A", "b|B", "c|C", "d|D", "e|E", "f|F"]
        )
    elif dataset == "logical_deduction_seven_objects":
        # do not strip the instruction
        prefix = "The following paragraphs each describe a set of seven objects arranged in a fixed order. The statements are logically consistent within each paragraph."
        task_description = prefix.strip()
        dataset = Dataset(
            "../../data/bbh",
            "logical_deduction_seven_objects",
            seed,
        )
        output_classes = OutputClasses(
            protos=["a|A", "b|B", "c|C", "d|D", "e|E", "f|F", "g|G"]
        )
    return prefix, task_description, dataset, output_classes, val_examples


def validate(dataset, model, loss_fn, iteration, val_examples, val_scores, writer):
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
            total=dataset.get_size("dev") if val_examples < 0 else val_examples,
            bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}",
            desc="Eval",
        )
        dataset.reset_pointer("dev")
        num_examples = 0

        for batch in dataset.iterate("dev", batch_size=20):
            x, y = batch
            y_hat = model.forward(np.array(x))
            acc += len(y) - np.sum(loss_fn(y_hat, y))
            tot += len(y)
            pbar.update(len(y))
            pbar.set_postfix_str(f"{acc / tot:.1%}")
            num_examples += len(y)

            if num_examples == val_examples:
                break
        dev_acc = acc / tot
        val_scores[val_key] = dev_acc

    if iteration == 0:
        log_message(colored("INIT DEV ACC: {}".format(dev_acc), "red"))
    log_message(colored("DEV ACC: {}".format(dev_acc), "red"))
    writer.add_scalar("dev/acc", (dev_acc), iteration)
    return dev_acc


def test(dataset, model, loss_fn, iteration, writer):
    log_message(colored("TESTING...", "red"))
    acc = 0.0
    tot = 0.0
    pbar = tqdm.tqdm(
        total=dataset.get_size("test"),
        bar_format="{l_bar}{bar:10}{r_bar}{bar:-10b}",
        desc="Eval",
    )

    dataset.reset_pointer("test")
    for batch in dataset.iterate("test", batch_size=20):
        x, y = batch
        y_hat = model.forward(np.array(x))
        acc += len(y) - np.sum(loss_fn(y_hat, y))
        tot += len(y)
        pbar.update(len(y))
        pbar.set_postfix_str(f"{acc / tot:.1%}")

    test_acc = acc / tot
    writer.add_scalar("test/acc", (test_acc), iteration)

    return test_acc


@click.command()
@click.option("--seed", default=42, help="Random seed.")
@click.option("--out_dir", default="log/")
@click.option("--val_freq", default=2)
@click.option("--do_first_eval", is_flag=True)
@click.option("--do_zero_shot", is_flag=True)
@click.option("--q_hidden", default="suffix_forward_tbs")
@click.option("--q_prompt", default="q_action_prompt")
@click.option("--p_hidden", default="suffix_forward_tbs")
@click.option("--p_class", default="classify_forward")
@click.option("--balance_batch", is_flag=True, help="Balance batch.")
@click.option("--batch_size", type=int, default=20)
@click.option("--one_layer", is_flag=True)
@click.option("--dataset", type=str, default="subj")
@click.option("--use_h_argmax", type=bool, default=False)
@click.option("--iters", type=int, default=20)
@click.option("--num_p_samples", type=int, default=5)
@click.option("--num_h_samples", type=int, default=3)
@click.option("--tolerance", type=int, default=-1)
@click.option(
    "--strip_options_for_hidden",
    type=bool,
    default=False,
    help="Remove options from examples for the hidden layer.",
)
@click.option(
    "--strip_prefix_for_hidden",
    type=bool,
    default=False,
    help="Strip the prefix from the examples if it exists in some tasks, e.g. BBH.",
)
@click.option(
    "--strip_answer_for_hidden",
    type=bool,
    default=False,
    help="Strip the 'Answer:' from the hidden state, if the model generates it.",
)
@click.option(
    "--trust_factor",
    default=0.0,
    help="Trust-region factor for prompt update. Ensures KL divergence between the old and new prompt is small.",
)
@click.option(
    "--fwd_temp",
    default=0.,
    help="Forward temperature",
)
@click.option(
    "--bwd_temp",
    default=0.7,
    help="Backward temperature",
)
@click.option(
    "--one_batch", type=float, default=0.0, help="Run only one batch, debug mode."
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
    default=False,
    help="Uses classes in the forward pass, constrains the output space.",
)
@click.option(
    "--init_p1", type=str, default="Decompose the problem to make it simpler:",
)
@click.option(
    "--init_p2", type=str, default=None,
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
    default=0.,
    help="Logp penalty for hiddens that haven't worked. Encourages exploration.",
)
@click.option(
    "--decay_logp_penalty",
    type=bool,
    default=True,
    help="Decay logp penalty linearly, reaching zero at the last iteration.",
)
@click.option(
    "--posterior_temp",
    type=float,
    default=1.0,
    help="Sharpen (<1.0)/Flatten (>1.0) the posterior distribution over h.",
)
@click.option(
    "--model_type",
    type=str,
    default="text-davinci-003",
)
@click.option(
    "--nce",
    type=bool,
    default=False,
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
def main(
    seed,
    out_dir,
    val_freq,
    do_first_eval,
    do_zero_shot,
    q_hidden,
    q_prompt,
    p_hidden,
    p_class,
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
    strip_answer_for_hidden,
    strip_prefix_for_hidden,
    trust_factor,
    one_batch,
    use_memory,
    init_p1,
    init_p2,
    tolerance,
    forward_use_classes,
    held_out_prompt_ranking,
    train_p1,
    train_p2,
    logp_penalty,
    decay_logp_penalty,
    posterior_temp,
    model_type,
    nce,
    fwd_max_tokens,
    bwd_max_tokens,
):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")
    out_dir = f"{out_dir}/{timestamp}"
    os.makedirs(out_dir, exist_ok=True)

    logging.basicConfig(
        filename=f"{out_dir}/output.log",
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_message(json.dumps(locals()))
    log_message("Logging to... {}".format(out_dir + "/output.log"))

    writer = SummaryWriter(f"{out_dir}")

    init_p1, init_p2 = init_prompts(dataset, init_p1, init_p2)

    prefix, task_description, dataset, output_classes, val_examples = init_dataset(dataset, seed)

    forward_instantiate(
        model_type,
        temperature=0.,
        max_tokens=fwd_max_tokens,
        stop=None,
    )
    backward_instantiate(
        model_type,
        temperature=bwd_temp,
        max_tokens=bwd_max_tokens,
        stop=None,
    )

    loss_fn = ZeroOneLoss(postproc=postprocess_prediction)
    model = VILModel(
        loss_fn,
        task_description=task_description,
        two_layers=not one_layer,
        num_p_samples=num_p_samples,
        num_h_samples=num_h_samples,
        q_hidden=q_hidden,
        q_prompt=q_prompt,
        p_hidden=p_hidden,
        p_class=p_class,
        init_p1=init_p1,
        init_p2=init_p2,
        nce=nce,
        use_h_argmax=use_h_argmax,
        output_classes=output_classes,
        strip_options_for_hidden=strip_options_for_hidden,
        strip_answer_for_hidden=strip_answer_for_hidden,
        trust_factor=trust_factor,
        forward_use_classes=forward_use_classes,
        held_out_prompt_ranking=held_out_prompt_ranking,
        use_memory=use_memory,
        train_p1=train_p1,
        train_p2=train_p2,
        logp_penalty=logp_penalty,
        p1_max_tokens=256,
        p2_max_tokens=20,
        posterior_temp=posterior_temp,
        strip_prefix_for_hidden=prefix if strip_prefix_for_hidden else None,
    )

    running_acc = 0.0
    running_elbo = 0.0
    best_dev = 0.0
    best_ps = [model.encoder_l1.weight, model.encoder_l2.weight]
    train_x, train_y = None, None
    sample_next_batch = False
    val_scores = {}

    patience = 0
    for iteration in range(iters + 1):
        log_message("STARTING EPOCH {} - {}".format(iteration, out_dir))

        if iteration % val_freq == 0 and (iteration > 0 or do_first_eval or do_zero_shot):
            dev_acc = validate(dataset, model, loss_fn, iteration, val_examples, val_scores, writer)
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

        # zero shot or allow last iteration for validation
        if do_zero_shot or iteration == iters:
            break

        if one_batch > 0.0 and train_x is not None and not sample_next_batch:
            # use the same batch, just re-shuffle the examples in the batch
            permutation_indices = np.random.permutation(np.arange(len(train_x)))
            x, y = np.asarray([train_x[i] for i in permutation_indices]), np.asarray(
                [train_y[i] for i in permutation_indices]
            )
            log_message(colored("USING SAME BATCH FOR TRAINING!!!", "yellow"))
        else:
            x, y = dataset.get_batch(
                "train", batch_size, random_sample=True, balance=balance_batch
            )
            train_x, train_y = x, y

        if decay_logp_penalty:
            model.logp_penalty = logp_penalty * (1.0 - (iteration / iters))

        log_message(colored("Training P2? {}".format(model.train_p2), "red"))
        log_message(colored("LOGPenalty? {}".format(model.logp_penalty), "red"))
        elbo, p1, p2, loss, elbo1, elbo2 = model.forward(np.array(x), np.array(y), temperature=fwd_temp)
    
        # Update prompts
        model.encoder_l1.weight = p1
        model.encoder_l2.weight = p2
        log_message("Patience: {}".format(patience))

        if iteration == 0:
            running_elbo = elbo
            running_acc = 1.0 - loss
        else:
            running_elbo = 0.2 * elbo + 0.8 * running_elbo
            running_acc = 0.2 * (1.0 - loss) + 0.8 * running_acc

        # get another batch if training accuracy is too good!
        sample_next_batch = (1.0 - loss) > one_batch

        log_message("--------------------")
        log_message(colored("{} TRAINING EPOCH DONE.".format(iteration), "blue"))
        log_message(colored("ELBO: {}".format(elbo), "blue"))
        log_message(colored("ACC: {}".format((1.0 - loss)), "blue"))
        log_message(colored("RUN ELBO: {}".format(running_elbo), "blue"))
        log_message(colored("RUN ACC: {}".format(running_acc), "blue"))
        log_message(colored("BATCH Y BALANCE: {}".format(Counter(y)), "blue"))
        log_message(colored("BATCH X LEN: {}".format([len(x_i) for x_i in x]), "blue"))

        writer.add_scalar("elbo", elbo, iteration)
        writer.add_scalar("elbo1", elbo1, iteration)
        writer.add_scalar("elbo2", elbo2, iteration)
        writer.add_scalar("acc", (1.0 - loss), iteration)

    log_message("--------------------")
    log_message("Loading best model...")

    model.encoder_l1.weight = best_ps[0]
    model.encoder_l2.weight = best_ps[1]

    log_message("Best L1 weights:", model.encoder_l1.weight)
    log_message("Best L2 weights:", model.encoder_l2.weight)

    test_acc = test(dataset, model, loss_fn, iteration, writer)

    log_message(colored("DEV ACC: {}".format(best_dev), "green"))
    log_message(colored("TEST ACC: {}".format(test_acc), "green"))
    writer.close()


if __name__ == "__main__":
    main()