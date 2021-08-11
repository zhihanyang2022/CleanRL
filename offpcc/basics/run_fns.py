import gin
import time
import datetime
import wandb
import csv
import os
import time

from typing import Union
from copy import deepcopy
import numpy as np
from gym.wrappers import Monitor
import cv2

from basics.abstract_algorithms import OffPolicyRLAlgorithm, RecurrentOffPolicyRLAlgorithm
from basics.replay_buffer import ReplayBuffer
from basics.replay_buffer_recurrent import RecurrentReplayBufferGlobal

BASE_LOG_DIR = '../results'


def make_log_dir(env_name, algo_name, run_id) -> str:
    log_dir = f'{BASE_LOG_DIR}/{env_name}/{algo_name}/{run_id}'
    return log_dir


def test_for_one_episode(env, algorithm, render=False, env_from_dmc=False, render_pixel_state=False) -> tuple:

    """
    This function is too versatile, so it deserves some good documentation.

    There are 3 usages of this function:

    (1) During training. After each epoch, the algorithm is tested using this function.
        render, env_from_dmc and render_pixel_state are set to False.

    (2) Visualizing learned policy.
        render is set to True, env_from_dmc can be True or False (determined automatically from env name),
        render_pixel_state is set to False

    (3) Used in render_pixel_state_for_dmc_img_envs.py.
        render, env_from_dmc and render_pixel_state are all set to True

    @param env:
    @param algorithm:
    @param render:
    @param env_from_dmc: if True, rely on opencv for rendering
    @param render_pixel_state: if True, display an image made up of 3 concatenated greyscale images
    @return:
    """

    state, done, episode_return, episode_len, episode_success = env.reset(), False, 0, 0, None

    if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):
        algorithm.reinitialize_hidden()  # crucial, crucial step for recurrent agents

    if render and env_from_dmc:
        cv2.namedWindow('img', cv2.WINDOW_NORMAL)

    cnt = 0
    import os

    while not done:
        action = algorithm.act(state, deterministic=True)
        cnt += 1

        num_files = len([fname for fname in os.listdir(f"../results/latent_vecs/vecs/") if fname.endswith("npy")]) // 3
        np.save(f"../results/latent_vecs/vecs/{num_files+1}", algorithm.hidden[1][1].squeeze(0).numpy())
        np.save(f"../results/latent_vecs/vecs/{num_files+1}_timestep", np.array([cnt]))
        np.save(f"../results/latent_vecs/vecs/{num_files+1}_label", np.array([env.option_idx]))

        state, reward, done, info = env.step(action)
        if render:
            if env_from_dmc:

                # thanks to Basj's answer from
                # https://stackoverflow.com/questions/53324068/a-faster-refresh-rate-with-plt-imshow

                # from opencv doc:
                # - If the image is 8-bit unsigned, it is displayed as is.
                # - If the image is 16-bit unsigned or 32-bit integer, the pixels are divided by 256. That is, the value range [0,255*256] is mapped to [0,255].
                # - If the image is 32-bit or 64-bit floating-point, the pixel values are multiplied by 255. That is, the value range [0,1] is mapped to [0,255].

                if render_pixel_state:
                    image = np.moveaxis(np.float32(state), 0, -1)  # state is already normalized to [0, 1], so we convert to 32-bit floating point
                else:
                    image = np.uint8(env.render(mode='rgb_array'))  # state is not normalized, so we convert to 8-bit unsigned

                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)  # cv2 uses BGR instead of RGB

                image = cv2.resize(image, (300, 300))  # otherwise the window is so small

                kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
                image = cv2.filter2D(image, -1, kernel)  # sharpen the image for a little bit

                cv2.imshow('img', image)
                cv2.waitKey(1)

            else:
                env.render()
        episode_return += reward
        episode_len += 1
    return episode_len, episode_return


def remove_jsons_from_dir(directory):
    for fname in os.listdir(directory):
        if fname.endswith('.json'):
            os.remove(os.path.join(directory, fname))


def load_and_visualize_policy(
        env,
        algorithm,
        log_dir,
        num_episodes,
        save_videos
) -> None:

    algorithm.load_actor(log_dir)

    if save_videos:  # no rendering in this case for speed

        env = Monitor(
            env,
            directory=f'{log_dir}/videos/',
            video_callable=lambda episode_id: True,  # record every single episode
            force=True
        )

        assert not env.spec.id.startswith("dmc"), "cannot record video for envs converted from dm_control, sorry!"

        for i in range(num_episodes):
            test_for_one_episode(env, algorithm, render=False)

        remove_jsons_from_dir(f'{log_dir}/videos/')

    else:

        ep_lens, ep_rets = [], []
        for i in range(num_episodes):
            ep_len, ep_ret = test_for_one_episode(env, algorithm, render=True, env_from_dmc=env.spec.id.startswith("dmc"))
            ep_lens.append(ep_len)
            ep_rets.append(ep_ret)

        print('===== Stats for sanity check =====')
        print('Episode returns:', [round(ret, 2) for ret in ep_rets])
        print('Episode lengths:', ep_lens)


@gin.configurable(module=__name__)
def train(
        env_fn,
        algorithm: Union[OffPolicyRLAlgorithm, RecurrentOffPolicyRLAlgorithm],
        buffer: Union[ReplayBuffer, RecurrentReplayBufferGlobal],
        num_epochs=gin.REQUIRED,
        num_steps_per_epoch=gin.REQUIRED,
        num_test_episodes_per_epoch=gin.REQUIRED,
        update_every=1,  # number of env interactions between grad updates; but the ratio is locked to 1-to-1
        update_after=gin.REQUIRED,  # for exploration; no update & random action from action space
) -> None:

    """Follow from OpenAI Spinup's training loop style"""

    # prepare for logging (csv)

    csv_file = open(f'{wandb.run.dir}/progress.csv', 'w+')
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow([
        'epoch',
        'timestep',  # number of env interactions OR grad updates (both are equivalent; ratio 1:1)
        'train_ep_len',  # averaged across epoch
        'train_ep_ret',  # averaged across epoch
        'test_ep_len',  # averaged across epoch
        'test_ep_ret',  # averaged across epoch
    ])

    # prepare stats trackers

    episode_len = 0
    episode_ret = 0
    train_episode_lens = []
    train_episode_rets = []
    algo_specific_stats_tracker = []

    total_steps = num_steps_per_epoch * num_epochs

    start_time = time.perf_counter()

    # prepare environments

    env = env_fn()

    # pbc stands for pybullet custom (e.g., bumps normal, top plate)
    # when env is pbc, then we avoid testing entirely

    if env.spec.id.startswith("pbc") is False:
        test_env = env_fn()

    state = env.reset()

    # training loop

    if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):
        algorithm_clone = deepcopy(algorithm)  # algorithm is for action; algorithm_clone is for updates and testing

        # Since algorithm is a recurrent policy, it (ideally) shouldn't be updated during an episode since this would
        # affect its ability to interpret past hidden states. Therefore, during an episode, algorithm_clone is updated
        # while algorithm is not. Once an episode has finished, we do algorithm = deepcopy(algorithm_clone) to carry
        # over the changes.

    for t in range(total_steps):

        # action = algorithm.act(state, deterministic=False)

        if t >= update_after:  # exploration is done
            action = algorithm.act(state, deterministic=False)
        else:
            action = env.action_space.sample()

        next_state, reward, done, info = env.step(action)
        episode_len += 1

        if env.spec.id.startswith("pbc"):
            episode_ret += 0 if reward <= 0 else 1  # so that punishment reward is recorded as 0 (success rate)
        else:
            episode_ret += reward

        # carefully decide what "done" should be at max_episode_steps

        if episode_len == env.spec.max_episode_steps:

            # here's how truncated is computed behind the scene
            # - at max_episode_steps & done=True -> truncated=False
            # - at max_episode_steps & done=False -> truncated=True
            # better than SpinUp's way, since SpinUp assumes truncated whenever at max_episode_steps
            # ref: https://github.com/openai/gym/blob/master/gym/wrappers/time_limit.py#L14 for calculation of truncated

            cutoff = info.get('TimeLimit.truncated')  # this key is only available at max_steps_per_episode
            done = False if cutoff else True

            assert done or cutoff, "Both done and cutoff are false at max_episode_steps"

        else:

            # when not at max_episode_steps, done's given by the original env and the TimeLimit wrapper are the same
            # caution: by original env I mean the env NOT wrapped by a TimeLimit wrapper; by default all envs from
            # OpenAI gym are wrapped by a TimeLimit wrapper

            cutoff = False

        # store the transition
        if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):
            buffer.push(state, action, reward, next_state, done, cutoff)
        elif isinstance(algorithm, OffPolicyRLAlgorithm):
            buffer.push(state, action, reward, next_state, done)

        # crucial, crucial preparation for next step
        state = next_state

        # end of trajectory handling
        if done or cutoff:

            train_episode_lens.append(episode_len)
            train_episode_rets.append(episode_ret)
            state, episode_len, episode_ret = env.reset(), 0, 0  # reset state and stats trackers

            if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):

                algorithm.copy_networks_from(algorithm_clone)  # doing a deepcopy will ruin the action noise schedule
                algorithm.reinitialize_hidden()  # crucial, crucial step for recurrent agents

        # update handling
        if t >= update_after and (t + 1) % update_every == 0:
            for j in range(update_every):

                batch = buffer.sample()

                if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):
                    algo_specific_stats = algorithm_clone.update_networks(batch)
                elif isinstance(algorithm, OffPolicyRLAlgorithm):
                    algo_specific_stats = algorithm.update_networks(batch)

                algo_specific_stats_tracker.append(algo_specific_stats)

        # end of epoch handling
        if (t + 1) % num_steps_per_epoch == 0:

            epoch = (t + 1) // num_steps_per_epoch

            # algo specific stats

            algo_specific_stats_over_epoch = {}

            if len(algo_specific_stats_tracker) != 0:
                # get keys from the first one; all dicts SHOULD share the same keys
                keys = algo_specific_stats_tracker[0].keys()
                for k in keys:
                    values = []
                    for dictionary in algo_specific_stats_tracker:
                        values.append(dictionary[k])
                    algo_specific_stats_over_epoch[k] = np.mean(values)
                algo_specific_stats_tracker = []

            # training stats

            mean_train_episode_len = np.mean(train_episode_lens)
            mean_train_episode_ret = np.mean(train_episode_rets)

            train_episode_lens = []
            train_episode_rets = []

            # testing stats

            if env.spec.id.startswith("pbc"):

                mean_test_episode_len = -999  # just ignore this value
                mean_test_episode_ret = -999

            else:

                test_episode_lens, test_episode_returns = [], []

                for j in range(num_test_episodes_per_epoch):

                    if isinstance(algorithm, RecurrentOffPolicyRLAlgorithm):
                        test_algorithm = deepcopy(algorithm_clone)
                    elif isinstance(algorithm, OffPolicyRLAlgorithm):
                        test_algorithm = deepcopy(algorithm)

                    test_episode_len, test_episode_return = test_for_one_episode(test_env, test_algorithm)
                    test_episode_lens.append(test_episode_len)
                    test_episode_returns.append(test_episode_return)

                mean_test_episode_len = np.mean(test_episode_lens)
                mean_test_episode_ret = np.mean(test_episode_returns)

            # time-related stats

            epoch_end_time = time.perf_counter()
            time_elapsed = epoch_end_time - start_time  # in seconds
            avg_time_per_epoch = time_elapsed / epoch  # in seconds
            num_epochs_to_go = num_epochs - epoch
            time_to_go = int(num_epochs_to_go * avg_time_per_epoch)  # in seconds
            time_to_go_readable = str(datetime.timedelta(seconds=time_to_go))

            # actually record / print the stats

            # (wandb logging)

            dict_for_wandb = {}

            if env.spec.id.startswith("pbc"):  # for publication purpose
                dict_for_wandb.update({
                    'Success Rate': mean_train_episode_ret,
                    'Episode Length': mean_train_episode_len,
                    'Hours': time_elapsed / 60 / 60
                })
            else:  # what happens normally
                dict_for_wandb.update({
                    'epoch': epoch,
                    'timestep': t+1,
                    'train_ep_len': mean_train_episode_len,
                    'train_ep_ret': mean_train_episode_ret,
                    'test_ep_len': mean_test_episode_len,
                    'test_ep_ret': mean_test_episode_ret,
                })

            dict_for_wandb.update(algo_specific_stats_over_epoch)  # for debugging

            if env.spec.id.startswith("pbc"):
                wandb.log(dict_for_wandb, step=t+1)
            else:
                wandb.log(dict_for_wandb)

            # (csv logging - will be uploaded to wandb at the very end)

            csv_writer.writerow([
                epoch,
                t + 1,
                mean_train_episode_len,
                mean_train_episode_ret,
                mean_test_episode_len,
                mean_test_episode_ret,
            ])

            # (console logging)

            stats_string = (
                f"===============================================================\n"
                f"| Epoch        | {epoch}\n"
                f"| Timestep     | {t+1}\n"
                f"| Train ep len | {round(mean_train_episode_len, 2)}\n"
                f"| Train ep ret | {round(mean_train_episode_ret, 2)}\n"
                f"| Test ep len  | {round(mean_test_episode_len, 2)}\n"
                f"| Test ep ret  | {round(mean_test_episode_ret, 2)}\n"
                f"| Time rem     | {time_to_go_readable}\n"
                f"==============================================================="
            )  # this is a weird syntax trick but it just creates a single string
            print(stats_string)

    # save stats and model after training loop finishes
    algorithm.save_actor(wandb.run.dir)  # will get uploaded to cloud after script finishes
    csv_file.close()
