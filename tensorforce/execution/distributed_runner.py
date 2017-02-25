# Copyright 2016 reinforce.io. All Rights Reserved.
# ==============================================================================
"""
Coordinator for running distributed tensorflow. Starts multiple worker processes, which
themselves use execution classes. Runners can be threaded for realtime usage (e.g. OpenAI universe)
"""

from copy import deepcopy
from multiprocessing import Process
import time
import tensorflow as tf
import sys
import os

from tensorforce.agents.distributed_agent import DistributedAgent
from tensorforce.execution.thread_runner import ThreadRunner

class DistributedRunner(object):
    def __init__(self, agent_type, agent_config, n_agents, n_param_servers, environment,
                 episodes, max_timesteps, preprocessor=None, repeat_actions=1):

        self.agent_type = agent_type
        self.agent_config = agent_config
        self.n_agents = n_agents
        self.n_param_servers = n_param_servers
        self.environment = environment
        self.episodes = episodes
        self.max_timesteps = max_timesteps

        self.preprocessor = preprocessor
        self.repeat_actions = repeat_actions

        port = 12222

        ps_hosts = []
        worker_hosts = []

        for _ in range(self.n_param_servers):
            ps_hosts.append('127.0.0.1:{}'.format(port))
            port += 1

        for _ in range(self.n_agents):
            worker_hosts.append('127.0.0.1:{}'.format(port))
            port += 1

        cluster = {'ps': ps_hosts, 'worker': worker_hosts}

        self.cluster_spec = tf.train.ClusterSpec(cluster)

    def run(self):
        """
        Creates and starts worker processes and parameter servers.
        """
        self.processes = []

        for index in range(self.n_param_servers):
            process = Process(target=process_worker, args=(self, index, self.episodes, self.max_timesteps, True))
            self.processes.append(process)

            process.start()

        for index in range(self.n_agents):
            process = Process(target=process_worker, args=(self, index, self.episodes, self.max_timesteps, False))
            self.processes.append(process)

            process.start()


def process_worker(master, index, episodes, max_timesteps, is_param_server=False):
    """
    Process execution loop.

    :param master:
    :param index:
    :param episodes:
    :param max_timesteps:
    :param is_param_server:

    """
    # if not master.continue_execution:
    #     return
    sys.stdout = open('worker_' + str(index) + '.out', 'w')
    cluster = master.cluster_spec.as_cluster_def()

    if is_param_server:
        server = tf.train.Server(cluster, job_name='ps', task_index=index,
                                 config=tf.ConfigProto(device_filters=["/job:ps"]))
        #               config=tf.ConfigProto(allow_soft_placement=True)
        # Param server does nothing actively
        server.join()
    else:
        # Worker creates runner for execution
        scope = 'worker_' + str(index)

        server = tf.train.Server(cluster, job_name='worker', task_index=index,
                                 config=tf.ConfigProto(intra_op_parallelism_threads=1,
                                                       inter_op_parallelism_threads=2,
                                                       log_device_placement=True))
        #                              allow_soft_placement = True))

        worker_agent = DistributedAgent(master.agent_config, scope, index, cluster)

        def init_fn(session):
            session.run(worker_agent.model.init_op)

        # init op problematic
        #config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(index)])
        config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(index)])

        supervisor = tf.train.Supervisor(is_chief=(index == 0),
                                         logdir="/tmp/train_logs",
                                         init_op=tf.global_variables_initializer(),
                                         # init_fn=init_fn,
                                         summary_op=tf.summary.merge_all(),
                                         saver=worker_agent.model.saver,
                                         global_step=worker_agent.model.global_step,
                                         summary_writer=worker_agent.model.summary_writer)

        global_steps = 10000000
        runner = ThreadRunner(worker_agent, deepcopy(master.environment),
                              episodes, 20, preprocessor=master.preprocessor,
                              repeat_actions=master.repeat_actions)

        # config = tf.ConfigProto(allow_soft_placement=True)

        # Connecting to parameter server
        print('Connecting to session..')
        print('Server target = ' + str(server.target))
        with supervisor.managed_session(server.target, config=config) as session, session.as_default():
            print('Established session, starting runner..')

            runner.start_thread(session)
            global_step_count = worker_agent.increment_global_step()

            while not supervisor.should_stop() and global_step_count < global_steps:
                runner.update()
                global_step_count = worker_agent.increment_global_step()

        print('Stopping supervisor')
        supervisor.stop()


        # def get_episode_finished_handler(self, condition):
        #     def episode_finished(execution):
        #         condition.acquire()
        #         condition.wait()
        #         return self.continue_execution
        #     return episode_finished

        # def run(self, episodes, max_timesteps, episode_finished=None):
        #     self.total_states = 0
        #     self.episode_rewards = []
        #     self.continue_execution = True

        #     runners = []
        #     processes = []
        #     conditions = []
        #     for agent, environment in zip(self.agents, self.environments):
        #         condition = Condition()
        #         conditions.append(condition)

        #         execution = Runner(agent, environment, preprocessor=self.preprocessor, repeat_actions=self.repeat_actions)  # deepcopy?
        #         runners.append(execution)

        #         thread = Thread(target=execution.run, args=(episodes, max_timesteps), kwargs={'episode_finished': self.get_episode_finished_handler(condition)})
        #         processes.append(thread)
        #         thread.start()

        #     self.episode = 0
        #     loop = True
        #     while loop:
        #         for condition, execution in zip(conditions, runners):
        #             if condition._waiters:
        #                 self.timestep = execution.timestep
        #                 self.episode += 1
        #                 self.episode_rewards.append(execution.episode_rewards[-1])
        #                 # perform async update of parameters
        #                 # if T mod Itarget == 0:
        #                 #     update target network
        #                 # clear gradient
        #                 # sync parameters
        #                 condition.acquire()
        #                 condition.notify()
        #                 condition.release()
        #                 if self.episode >= episodes or (episode_finished and not episode_finished(self)):
        #                     loop = False
        #                     break
        #         self.total_states = sum(execution.total_states for execution in runners)

        #     self.continue_execution = False
        #     stopped = 0
        #     while stopped < self.n_runners:
        #         for condition, thread in zip(conditions, processes):
        #             if condition._waiters:
        #                 condition.acquire()
        #                 condition.notify()
        #                 condition.release()
        #                 conditions.remove(condition)
        #             if not thread.is_alive():
        #                 processes.remove(thread)
        #                 stopped += 1
