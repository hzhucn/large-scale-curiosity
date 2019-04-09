#!/usr/bin/env python
try:
    from OpenGL import GLU
except:
    print("no OpenGL.GLU")
import functools
import os.path as osp
from functools import partial

import sys, datetime

sys.path.append('/project/becker/lihuang3/github/baselines2')

import gym
import tensorflow as tf
import logger
from baselines.bench import Monitor
# from baselines.common.atari_wrappers import *
from atari_wrappers import *
from mpi4py import MPI

from auxiliary_tasks import FeatureExtractor, InverseDynamics, RandomNetworkDistillation, VAE, JustPixels
from cnn_policy import CnnPolicy
from cppo_agent import PpoOptimizer
from dynamics import Dynamics, RNDDyn, UNet
from utils import random_agent_ob_mean_std
from wrappers import MontezumaInfoWrapper, make_mario_env, make_robo_pong, make_robo_hockey, \
    make_multi_pong, AddRandomStateToInfo, MaxAndSkipEnv, ProcessFrame84, ExtraTimeLimit

from baselines.common.tf_util import get_session, save_variables, load_variables


def start_experiment(**args):
    make_env = partial(make_env_all_params, add_monitor=True, args=args)

    trainer = Trainer(make_env=make_env,
                      num_timesteps=args['num_timesteps'], hps=args,
                      envs_per_process=args['envs_per_process'])
    log, tf_sess = get_experiment_environment(**args)
    with log, tf_sess:
        logdir = logger.get_dir()
        print("results will be saved to ", logdir)
        trainer.train(args=args)


class Trainer(object):
    def __init__(self, make_env, hps, num_timesteps, envs_per_process):
        self.make_env = make_env
        self.hps = hps
        self.envs_per_process = envs_per_process
        self.num_timesteps = num_timesteps
        self._set_env_vars()

        self.policy = CnnPolicy(
            hps=hps,
            scope='pol',
            ob_space=self.ob_space,
            ac_space=self.ac_space,
            hidsize=512,
            feat_dim=512,
            ob_mean=self.ob_mean,
            ob_std=self.ob_std,
            layernormalize=False,
            nl=tf.nn.leaky_relu)

        self.feature_extractor = {"none": FeatureExtractor,
                                  "idf": InverseDynamics,
                                  "rnd": RandomNetworkDistillation,
                                  "vaesph": partial(VAE, spherical_obs=True),
                                  "vaenonsph": partial(VAE, spherical_obs=False),
                                  "pix2pix": JustPixels}[hps['feat_learning']]
        self.feature_extractor = self.feature_extractor(policy=self.policy,
                                                        features_shared_with_policy=False,
                                                        feat_dim=512,
                                                        layernormalize=hps['layernorm'])

        if hps['feat_learning']=='rnd': 
            self.dynamics = RNDDyn 
        elif hps['feat_learning'] == 'pix2pix': 
            self.dynamics = UNet
        else:
            self.dynamics = Dynamics
              
        self.dynamics = self.dynamics(auxiliary_task=self.feature_extractor,
                                      predict_from_pixels=hps['dyn_from_pixels'],
                                      feat_dim=512)

        self.agent = PpoOptimizer(
            hps=hps,
            scope='ppo',
            ob_space=self.ob_space,
            ac_space=self.ac_space,
            stochpol=self.policy,
            use_news=hps['use_news'],
            gamma=hps['gamma'],
            gamma_ext=hps['gamma_ext'],
            lam=hps["lambda"],
            nepochs=hps['nepochs'],
            nminibatches=hps['nminibatches'],
            lr=hps['lr'],
            cliprange=0.1,
            nsteps_per_seg=hps['nsteps_per_seg'],
            nsegs_per_env=hps['nsegs_per_env'],
            ent_coef=hps['ent_coeff'],
            normrew=hps['norm_rew'],
            normadv=hps['norm_adv'],
            ext_coeff=hps['ext_coeff'],
            int_coeff=hps['int_coeff'],
            dynamics=self.dynamics
        )

        if hps['feat_learning']!='rnd':
            self.agent.to_report['aux'] = tf.reduce_mean(self.feature_extractor.loss)
            self.agent.total_loss += self.agent.to_report['aux']
        self.agent.to_report['dyn_loss'] = tf.reduce_mean(self.dynamics.loss)
        self.agent.total_loss += self.agent.to_report['dyn_loss']
        self.agent.to_report['feat_var'] = tf.reduce_mean(tf.nn.moments(self.feature_extractor.features, [0, 1])[1])

    def _set_env_vars(self):
        env = self.make_env(0, add_monitor=False)
        self.ob_space, self.ac_space = env.observation_space, env.action_space
        self.ob_mean, self.ob_std = random_agent_ob_mean_std(env, nsteps=10000)
        del env
        self.envs = [functools.partial(self.make_env, i) for i in range(self.envs_per_process)]

    def train(self, args):
        self.agent.start_interaction(self.envs, nlump=self.hps['nlumps'], dynamics=self.dynamics)
        sess = tf.get_default_session()
        self.save = functools.partial(save_variables, sess=sess)
        self.load = functools.partial(load_variables, sess=sess)
        checkdir = osp.join(logger.get_dir(), 'checkpoints', args['env'])
        os.makedirs(checkdir, exist_ok=True)
        load_weights = args['load_weights']
        start_nupdates = 0
        if load_weights is not None:
            load_path = osp.join(checkdir, load_weights)
            start_nupdates = int(load_weights)
            print('Loading checkpoint from %s ' % load_weights)
            self.load(load_path)

        while True:
            info = self.agent.step()
            if info['update']:
                info['update']['n_updates'] += start_nupdates
                info['update']['tcount'] += start_nupdates*args['nsteps_per_seg']*args['envs_per_process']
                logger.logkvs(info['update'])
                logger.dumpkvs()
                print('Time elapsed ' + str(datetime.timedelta(seconds=info['update']['total_secs'])))                
                
                if info['update']['n_updates'] % 10 == 0 or info['update']['n_updates']==1: 
                    weights_index =  info['update']['n_updates']             
                    savepath = osp.join(checkdir, '%.5i'% weights_index)
                    print('Saving to', savepath)
                    self.save(savepath)

            if self.agent.rollout.stats['tcount'] > self.num_timesteps:
                break
        self.agent.stop_interaction()


def make_env_all_params(rank, add_monitor, args):
    if args["env_kind"] == 'atari':
        env = gym.make(args['env'])
        assert 'NoFrameskip' in env.spec.id
        env = NoopResetEnv(env, noop_max=args['noop_max'])
        env = MaxAndSkipEnv(env, skip=4)
        env = ProcessFrame84(env, crop=False)
        env = FrameStack(env, 4)
        env = ExtraTimeLimit(env, args['max_episode_steps'])
        if 'Montezuma' in args['env']:
            env = MontezumaInfoWrapper(env)
        env = AddRandomStateToInfo(env)
    elif args["env_kind"] == 'mario':
        env = make_mario_env()
    elif args["env_kind"] == "retro_multi":
        env = make_multi_pong()
    elif args["env_kind"] == 'robopong':
        if args["env"] == "pong":
            env = make_robo_pong()
        elif args["env"] == "hockey":
            env = make_robo_hockey()
    elif args["env_kind"] == "my_games":
        env = gym.make(args['env'])
        env = MaxAndSkipEnv(env, skip=4)
        env = WarpFrame(env, width=84, height=84)
        env = FrameStack(env, 4)

    if add_monitor:
        env = Monitor(env, osp.join(logger.get_dir(), '%.2i' % rank))
    return env


def get_experiment_environment(**args):
    from utils import setup_mpi_gpus, setup_tensorflow_session
    from baselines.common import set_global_seeds
    from gym.utils.seeding import hash_seed
    process_seed = args["seed"] + 1000 * MPI.COMM_WORLD.Get_rank()
    process_seed = hash_seed(process_seed, max_bytes=4)
    set_global_seeds(process_seed)
    setup_mpi_gpus()

    logger_context = logger.scoped_configure(dir=args['env'],
                                             format_strs=['stdout', 'log',
                                                          'csv'] if MPI.COMM_WORLD.Get_rank() == 0 else ['log'])
    tf_context = setup_tensorflow_session()
    return logger_context, tf_context


def add_environments_params(parser):
    # 'BreakoutNoFrameskip-v4'
    parser.add_argument('--env', help='environment ID', default= 'Maze1203Env-v1',
                        type=str)
    parser.add_argument('--max-episode-steps', help='maximum number of timesteps for episode', default=None, type=int)
    parser.add_argument('--env_kind', type=str, default="my_games")
    parser.add_argument('--noop_max', type=int, default=30)


def add_optimization_params(parser):
    parser.add_argument('--lambda', type=float, default=0.95)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--gamma_ext', type=float, default=0.99)    
    parser.add_argument('--nminibatches', type=int, default=16)
    parser.add_argument('--norm_adv', type=int, default=0)
    parser.add_argument('--norm_rew', type=int, default=1)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--ent_coeff', type=float, default=0.001)
    parser.add_argument('--nepochs', type=int, default=3)
    parser.add_argument('--num_timesteps', type=int, default=int(1e9))


def add_rollout_params(parser):
    parser.add_argument('--nsteps_per_seg', type=int, default=2000)
    parser.add_argument('--nsegs_per_env', type=int, default=1)
    parser.add_argument('--envs_per_process', type=int, default=128)
    parser.add_argument('--nlumps', type=int, default=1)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_environments_params(parser)
    add_optimization_params(parser)
    add_rollout_params(parser)

    parser.add_argument('--load_weights', type=str, default=None)
    parser.add_argument('--exp_name', type=str, default='')
    parser.add_argument('--seed', help='RNG seed', type=int, default=0)
    parser.add_argument('--dyn_from_pixels', type=int, default=0)
    parser.add_argument('--use_news', type=int, default=0)
    parser.add_argument('--ext_coeff', type=float, default=1.00)
    parser.add_argument('--int_coeff', type=float, default=0.5)
    parser.add_argument('--layernorm', type=int, default=0)
    parser.add_argument('--feat_learning', type=str, default='rnd',
                        choices=["none", "idf", "rnd", "vaesph", "vaenonsph", "pix2pix"])
    parser.add_argument('--num_vf', type=int, default=1, choices=[1,2])

    args = parser.parse_args()

    start_experiment(**args.__dict__)
