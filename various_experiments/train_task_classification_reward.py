from tigr.task_inference.dpmm_inference import DecoupledEncoder
# from configs.toy_config import toy_config
import numpy as np
from rlkit.envs import ENVS
from tigr.task_inference.dpmm_bnp import BNPModel
import torch
import os
from rlkit.torch.sac.policies import TanhGaussianPolicy
from sac_envs.half_cheetah_multi import HalfCheetahMixtureEnv
from model import PolicyNetwork as TransferFunction
import rlkit.torch.pytorch_util as ptu
from collections import OrderedDict
import cv2
from typing import List, Any, Dict, Callable
import json
import imageio
import rlkit.torch.pytorch_util as ptu
from tigr.task_inference.prediction_networks import DecoderMDP, ExtendedDecoderMDP
import matplotlib.pyplot as plt
import random
from collections import namedtuple
import torch.nn as nn
import torch.optim as optim

from agent import SAC
from model import ValueNetwork, QvalueNetwork, PolicyNetwork

# DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
DEVICE = 'cuda'
ptu.set_gpu_mode(True)

# TODO: einheitliches set to device
simple_env_dt = 0.05
sim_time_steps = 10
max_path_len=100

loss_criterion = nn.CrossEntropyLoss()



# TODO: self.task_dim, task_logits_dim, batch_size are set manually
class Memory():

    def __init__(self, memory_size):
        self.memory_size = memory_size
        self.memory = []
        self.Transition = namedtuple('Transition',
                        ('task', 'simple_obs', 'simple_action', 'mu'))
        self.batch_size = 256
        self.task_dim = 1
        self.latent_dim = 4
        self.simple_obs_dim = 2
        self.simple_action_dim = 1

    def add(self, *transition):
        self.memory.append(self.Transition(*transition))
        if len(self.memory) > self.memory_size:
            self.memory.pop(0)
        assert len(self.memory) <= self.memory_size

    def sample(self, size):
        return random.sample(self.memory, size)

    def __len__(self):
        return len(self.memory)
    
    def unpack(self, batch):
        batch = self.Transition(*zip(*batch))
        
        tasks = torch.cat(batch.task).view(self.batch_size, self.task_dim).to(DEVICE)
        simple_obs = torch.cat(batch.simple_obs).view(self.batch_size, self.simple_obs_dim).to(DEVICE)
        simple_action = torch.cat(batch.simple_action).view(self.batch_size, self.simple_action_dim).to(DEVICE)
        mu = torch.cat(batch.mu).view(self.batch_size, self.latent_dim).to(DEVICE)

        return tasks, simple_obs, simple_action, mu


def log_all(agent, path, q1_loss, policy_loss, rew, episode):
    '''
    # Save under structure:
    # - {os.getcwd()}/experiments_transfer_function/<name_of_experiment>
    #     - plots
    #         - mean_reward_history
    #         - qf_loss
    #         - policy_loss
    #     - models
    #         - transfer_function / policy_net
    #         - qf1
    #         - value
    '''

    # TODO: save both vf losses (maybe with arg)
    def save_plot(loss_history, name:str, path=f'{os.getcwd()}/evaluation/transfer_function/one-sided/'):
        os.makedirs(os.path.dirname(path), exist_ok=True)

        plt.figure()
        # Plotting the loss
        plt.plot(loss_history)
        plt.title('Loss over Time')
        plt.xlabel('Epoch')
        plt.ylabel('Loss')
        plt.savefig(path+name+'.png')

        plt.close()
        
    # Save networks
    curr_path = path + '/models/policy_model/'
    os.makedirs(os.path.dirname(curr_path), exist_ok=True)
    save_path = curr_path + f'weights.pth'
    if episode % 500 == 0:
        torch.save(agent.policy_network.cpu(), save_path)
    curr_path = path + '/models/vf1_model/'
    os.makedirs(os.path.dirname(curr_path), exist_ok=True)
    save_path = curr_path + f'weights.pth'
    if episode % 500 == 0:
        torch.save(agent.q_value_network1.cpu(), save_path)
    curr_path = path + '/models/vf2_model/'
    os.makedirs(os.path.dirname(curr_path), exist_ok=True)
    save_path = curr_path + f'weights.pth'
    if episode % 500 == 0:
        torch.save(agent.q_value_network2.cpu(), save_path)
    curr_path = path + '/models/value_model/'
    os.makedirs(os.path.dirname(curr_path), exist_ok=True)
    save_path = curr_path + f'weights.pth'
    if episode % 500 == 0:
        torch.save(agent.value_network.cpu(), save_path)
    agent.q_value_network1.cuda() 
    agent.q_value_network2.cuda()
    agent.value_network.cuda()
    agent.policy_network.cuda() 

    # Save plots
    path_plots = path + '/plots/'
    save_plot(q1_loss, name='vf_loss', path=path_plots)
    save_plot(rew, name='reward_history', path=path_plots)
    save_plot(policy_loss, name='policy_loss', path=path_plots)

def get_encoder(path, shared_dim, encoder_input_dim):
    path = os.path.join(path, 'weights')
    for filename in os.listdir(path):
        if filename.startswith('encoder'):
            name = os.path.join(path, filename)
    
    # Important: Gru and Conv only work with trajectory encoding
    if variant['algo_params']['encoder_type'] in ['gru'] and variant['algo_params']['encoding_mode'] != 'trajectory':
        print(f'\nInformation: Setting encoding mode to trajectory since encoder type '
              f'"{variant["algo_params"]["encoder_type"]}" doesn\'t work with '
              f'"{variant["algo_params"]["encoding_mode"]}"!\n')
        variant['algo_params']['encoding_mode'] = 'trajectory'
    elif variant['algo_params']['encoder_type'] in ['transformer', 'conv'] and variant['algo_params']['encoding_mode'] != 'transitionSharedY':
        print(f'\nInformation: Setting encoding mode to trajectory since encoder type '
              f'"{variant["algo_params"]["encoder_type"]}" doesn\'t work with '
              f'"{variant["algo_params"]["encoding_mode"]}"!\n')
        variant['algo_params']['encoding_mode'] = 'transitionSharedY'

    encoder = DecoupledEncoder(
        shared_dim,
        encoder_input_dim,
        num_classes = variant['reconstruction_params']['num_classes'],
        latent_dim = variant['algo_params']['latent_size'],
        time_steps = variant['algo_params']['time_steps'],
        encoding_mode=variant['algo_params']['encoding_mode'],
        timestep_combination=variant['algo_params']['timestep_combination'],
        encoder_type=variant['algo_params']['encoder_type'],
        bnp_model=bnp_model
    )
    encoder.load_state_dict(torch.load(name, map_location='cpu'))
    encoder.to(DEVICE)
    return encoder

def sample_task():
    goal_vel = np.random.choice([0,1,2,3])
    if goal_vel == 0:
        task = np.array([np.random.random()*15 + 1.0,0,0,0,0])
    elif goal_vel == 1:
        task = np.array([np.random.random()*15 - 16,0,0,0,0])
    elif goal_vel == 2:
        task = np.array([0,0,0,np.random.random()*2 + 1, 0])
    else:
        task = np.array([0,0,0,np.random.random()*2 - 3, 0])
    return task

def get_simple_agent(path, obs_dim, policy_latent_dim, action_dim, m):
    path = os.path.join(path, 'weights')
    for filename in os.listdir(path):
        if filename.startswith('policy'):
            name = os.path.join(path, filename)
    
    policy = TanhGaussianPolicy(
        obs_dim=(obs_dim + policy_latent_dim),
        action_dim=action_dim,
        latent_dim=policy_latent_dim,
        hidden_sizes=[m,m,m],
    )
    policy.load_state_dict(torch.load(name, map_location='cpu'))
    policy.to(DEVICE)
    return policy



def get_complex_agent(env, complex_agent_config):
    pretrained = complex_agent_config['experiments_repo']+complex_agent_config['experiment_name']+f"/models/policy_model/epoch_{complex_agent_config['epoch']}.pth"
    n_states = env.observation_space.shape[0]
    n_actions = env.action_space.shape[0]
    action_bounds = [env.action_space.low[0], env.action_space.high[0]]
    transfer_function = TransferFunction(
        n_states=n_states,
        n_actions=n_actions,
        action_bounds=action_bounds,
        pretrained=pretrained
        )
    transfer_function.to(DEVICE)
    return transfer_function

def cheetah_to_simple_env_map(
    # observations: torch.Tensor, 
    observations,
    next_observations: torch.Tensor):
    """
    Maps transitions from the cheetah environment
    to the discrete, one-dimensional goal environment.
    """

    ### little help: [0:3] gives elements in positions 0,1,2 
    simple_observations = np.zeros(obs_dim)
    simple_observations[...,0] = observations[...,-3]
    simple_observations[...,1] = observations[...,8]

    next_simple_observations = np.zeros(obs_dim)
    next_simple_observations[...,0] = next_observations[...,-3]
    next_simple_observations[...,1] = next_observations[...,8]

    return simple_observations, next_simple_observations

def cheetah_to_simple_env_obs(obs):
    simple_observations = np.zeros(obs_dim)
    simple_observations[...,0] = obs[...,-3]
    # simple_observations[...,1:3] = obs[...,1:3]
    # simple_observations[...,3:] = obs[...,7:10]
    simple_observations[...,1] = obs[...,8]
    return simple_observations

def scale_simple_action(simple_action, obs, pos_x=[0.5,25], velocity_x=[0.5, 3.0], pos_y=[np.pi / 5., np.pi / 2.], velocity_y=[2. * np.pi, 4. * np.pi], velocity_z=[1.5, 3.], step='set_position'):
    simple_env_dt = 0.05
    scaled_action = torch.zeros(2)
    scaled_action[0] = obs[0] + simple_action[0]*velocity_x[1]*simple_env_dt
    scaled_action[1] = simple_action[0]*velocity_x[1]

    return scaled_action


def _frames_to_gif(frames: List[np.ndarray], info, gif_path, transform: Callable = None):
    """ Write collected frames to video file """
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    with imageio.get_writer(gif_path, mode='I', fps=10) as writer:
        for i, frame in enumerate(frames):
            frame = frame.astype(np.uint8)  # Ensure the frame is of type uint8
            frame = np.ascontiguousarray(frame)
            cv2.putText(frame, 'reward: ' + str(info['reward'][i]), (0, 35), cv2.FONT_HERSHEY_TRIPLEX, 0.3, (0, 0, 255))
            cv2.putText(frame, 'obs: ' + str(info['obs'][i]), (0, 55), cv2.FONT_HERSHEY_TRIPLEX, 0.3, (0, 0, 255))
            cv2.putText(frame, 'simple_action: ' + str(info['simple_action'][i]), (0, 15), cv2.FONT_HERSHEY_TRIPLEX, 0.3, (0, 0, 255))
            cv2.putText(frame, 'task: ' + str(info['base_task'][i]), (0, 75), cv2.FONT_HERSHEY_TRIPLEX, 0.3, (0, 0, 255))
            # Apply transformation if any
            if transform is not None:
                frame = transform(frame)
            else:
                # Convert color space if no transformation provided
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            writer.append_data(frame)

def get_decoder(path, action_dim, obs_dim, reward_dim, latent_dim, output_action_dim, net_complex_enc_dec, variant):
    path = os.path.join(path, 'weights')
    for filename in os.listdir(path):
        if filename.startswith('decoder'):
            name = os.path.join(path, filename)
    output_action_dim = 8
    decoder = ExtendedDecoderMDP(
        action_dim,
        obs_dim,
        reward_dim,
        latent_dim,
        output_action_dim,
        net_complex_enc_dec,
        variant['env_params']['state_reconstruction_clip'],
    ) 

    decoder.load_state_dict(torch.load(name, map_location='cpu'))

    for param in decoder.parameters():
        param.requires_grad = False
    for param in decoder.task_decoder.last_fc.parameters():
        param.requires_grad = True

    decoder.to(DEVICE)
    return decoder

def create_tsne(latent_variables, task_labels, path):
    from sklearn.manifold import TSNE
    import matplotlib.pyplot as plt
    save_as = os.path.join(path , 'tsne_test.png')
    # Apply t-SNE
    tsne = TSNE(n_components=2, random_state=42)
    tsne_results = tsne.fit_transform(latent_variables)

    # Plot
    plt.figure(figsize=(10, 6))
    unique_labels = np.unique(task_labels)
    for label in unique_labels:
        idx = task_labels == label
        plt.scatter(tsne_results[idx, 0], tsne_results[idx, 1], label=label, alpha=0.7)
    plt.legend()
    plt.title('t-SNE Visualization of Latent Variables')
    plt.xlabel('t-SNE Dimension 1')
    plt.ylabel('t-SNE Dimension 2')
    plt.savefig(save_as)
    plt.close()

def save_plot(loss_history, name:str, path=f'{os.getcwd()}/plots'):
        os.makedirs(os.path.dirname(path), exist_ok=True)

        plt.figure()
        # Plotting the loss
        plt.plot(loss_history)
        plt.title('Loss over Time')
        plt.xlabel('Epoch')
        plt.ylabel('Loss')
        plt.savefig(os.path.join(path,name+'.png'))

        plt.close()

def step_cheetah(task, obs):

    for i in range(sim_time_steps):
            
            complex_action = transfer_function.get_action(ptu.from_numpy(obs), task, return_dist=False)
            next_obs, r, internal_done, truncated, env_info = env.step(complex_action.detach().cpu().numpy())

            # image = env.render()
            # frames.append(image)
            # image_info['reward'].append(r)
            # image_info['obs'].append(task)
            # image_info['base_task'].append(env.task)
            # if internal_done:
            #     break

            obs = next_obs
        
    return r, next_obs

def normalize_data(stats_dict, o, a, r, next_o):
        o = torch.Tensor((o - stats_dict['observations']['mean']) / (stats_dict['observations']['std'] + 1e-9))
        a = torch.Tensor((a - stats_dict['actions']['mean']) / (stats_dict['actions']['std'] + 1e-9))
        r = torch.Tensor((r - stats_dict['rewards']['mean']) / (stats_dict['rewards']['std'] + 1e-9))
        next_o = torch.Tensor((next_o - stats_dict['next_observations']['mean']) / (stats_dict['next_observations']['std'] + 1e-9))

        return o, a, r, next_o


def rollout(env, encoder, decoder, simple_agent, transfer_function_pos, transfer_function_vel, memory, 
            variant, obs_dim, actions_dim, max_path_len, 
            n_tasks, inner_loop_steps, save_video_path):
    range_dict = OrderedDict(pos_x = [0.5, 25],
                             velocity_z = [1.5, 3.],
                             pos_y = [np.pi / 6., np.pi / 2.],
                             velocity_x = [0.5, 3.0],
                             velocity_y = [2. * np.pi, 4. * np.pi],
                             )
    
    save_after_episodes = 5
    value_loss_history, q_loss_history, policy_loss_history, rew_history = [], [], [], []
    path = save_video_path
    
    # with open(f'{save_video_path}/weights/stats_dict.json', 'r') as file:
    #     stats_dict = json.load(file)

    loss_history = []
    reward_history = []

    decoder_value_loss, decoder_policy_loss, decoder_q_loss = [],[],[]

    for episode in range(30000):

        
        video = False
        if episode % 10 == 0:
            frames = []
            image_info = dict(reward = [],
            obs = [],
            base_task = [],
            complex_action = [],
            simple_action = [])
            video = True
            
        print(episode)

        path_length = 0
        obs = env.reset()[0]
        simple_env.reset_model()
        contexts = torch.zeros((n_tasks, variant['algo_params']['time_steps'], obs_dim + 1 + obs_dim), device=DEVICE)
        l_vars = []
        labels = []

        done = 0
        episode_reward = 0
        loss = []
        value_loss, q_loss, policy_loss = [], [], []
        task = env.sample_task()
        # env.update_task(task)


        _loss  = []
        for path_length in range(max_path_len):

            # get encodings
            encoder_input = contexts.detach().clone()
            encoder_input = encoder_input.view(encoder_input.shape[0], -1).to(DEVICE)
            mu, log_var = encoder(encoder_input)     # Is this correct??

            # Save values for plotting
            if env.task[0]<0:
                label = -1
            elif env.task[0]>0:
                label = 1
            elif env.task[3]<0:
                label = -2
            elif env.task[3]>0:
                label = 2
            if path_length == 0:
                l_vars = mu.detach().cpu().numpy()
                labels = np.array([label])
            else:
                l_vars = np.concatenate((l_vars, mu.detach().cpu().numpy()), axis = 0)
                labels = np.concatenate((labels, np.array([label])), axis = 0)


            obs_before_sim = env._get_obs()
            simple_obs_before = cheetah_to_simple_env_obs(obs_before_sim)

            # Save latent vars
            policy_input = torch.cat([ptu.from_numpy(simple_obs_before), mu.squeeze()], dim=-1)
            simple_action = simple_agent.get_torch_actions(policy_input, deterministic=True)
            
            logits = decoder.choose_action(np.array([np.zeros_like(obs_before_sim)]), mu.cpu().detach().numpy(), torch=True).squeeze()

            simple_obs,_,_,_ = simple_env.step(simple_action.detach().cpu().numpy())
            simple_obs = torch.Tensor([simple_obs[0], 0,0,simple_obs[1],0]).to(DEVICE)
            task_prediction = torch.argmax(torch.nn.functional.softmax(logits), dim=0)
            if task_prediction == 0 or task_prediction == 1:    # velocity_forward
                simple_obs[0] = 0
                transfer_function  = transfer_function_vel
            elif task_prediction == 2 or task_prediction == 3:
                simple_obs[3] = 0
                transfer_function  = transfer_function_pos

            for i in range(5):
                complex_action = transfer_function.get_action(ptu.from_numpy(obs), simple_obs, return_dist=False)
                next_obs, r, done, truncated, env_info = env.step(complex_action.detach().cpu().numpy())
                obs = next_obs
                if video:
                    image = env.render()
                    frames.append(image)
                    image_info['reward'].append(r)
                    image_info['obs'].append(cheetah_to_simple_env_map(obs_before_sim, obs)[0])
                    image_info['base_task'].append(env.task)
                    image_info['complex_action'].append(complex_action)
                    image_info['simple_action'].append(simple_obs)

            episode_reward += r
            task_idx = np.nonzero(env.task)
            if env.task[0] > 0:
                task_idx = 2
            elif env.task[0] < 0:
                task_idx = 3
            elif env.task[3] > 0:
                task_idx = 0
            elif env.task[3] < 0:
                task_idx = 1
            task = torch.tensor([task_idx]).to("cpu")
            filler = np.zeros_like(obs_before_sim)
            fillertwo = np.zeros_like(obs_before_sim)
            decoder.store(filler, r, done, np.array([logits.cpu().detach().numpy()]), fillertwo, mu.detach())
            # Train step predictor
            decoder_losses = decoder.train(episode, False)
            decoder_value_loss.append(decoder_losses[0])
            decoder_policy_loss.append(decoder_losses[2])
            decoder_q_loss.append(decoder_losses[1])



            # high_level_controller.store(obs_before_sim, r, done, action.cpu().detach().numpy().squeeze(), obs, mu.detach())
            # losses = high_level_controller.train(episode, False)

            simple_obs_after = cheetah_to_simple_env_obs(obs)
            simple_env.sim.data.set_joint_qpos('boxslideX', simple_obs_after[0])
            simple_env.sim.data.set_joint_qvel('boxslideX', simple_obs_after[1])
            
            data = torch.cat([ptu.from_numpy(simple_obs_before), torch.unsqueeze(torch.tensor(r, device=DEVICE), dim=0), ptu.from_numpy(simple_obs_after)], dim=0).unsqueeze(dim=0)
            context = torch.cat([contexts.squeeze(), data], dim=0)
            contexts = context[-time_steps:, :]
            contexts = contexts.unsqueeze(0).to(torch.float32)

            # if len(memory) < memory.batch_size:
            #     continue
            # else: 
            #     batch = memory.sample(memory.batch_size)
            #     tasks_batch, simple_obs_batch, simple_action_batch, mu_batch = memory.unpack(batch)
            #     # _,_, logits_batch = decoder.choose_action(obs, desired_state, torch=True).squeeze()*max_steps,1,max_steps)
            #     optimizer.zero_grad()
            #     loss.backward()
            #     optimizer.step()

            #     _loss.append(loss)

        reward_history.append(episode_reward)
        if len(_loss)>0:
            loss_history.append(torch.stack(_loss).mean().detach().cpu().numpy())


        if episode % save_after_episodes == 0 and episode!=0:
            log_all(decoder, save_video_path, decoder_value_loss, decoder_policy_loss, rew=reward_history, episode=episode)

        if video:
            # save_plot(np.array(loss_history), name='task_loss', path=save_video_path)
            size = frames[0].shape

            # Save to corresponding repo
            save_as = f'{save_video_path}/videos/transfer_{episode}.mp4'
            # Write frames to video
            _frames_to_gif(frames, image_info, save_as)

        

if __name__ == "__main__":
    from experiments_configs.half_cheetah_multi_env import config as env_config

    inference_path = '/home/ubuntu/juan/melts/output/toy1d-multi-task/2024_04_30_10_59_18_default_true_gmm'

    
    complex_agent_pos = dict(
        environment = HalfCheetahMixtureEnv(env_config),
        experiments_repo = '/home/ubuntu/juan/Meta-RL/experiments_transfer_function/',
        experiment_name = 'half_cheetah_dt0.01_mall_vel3',
        epoch = 900,
    )
    complex_agent_vel = dict(
        experiments_repo = '/home/ubuntu/juan/Meta-RL/experiments_transfer_function/',
        experiment_name = 'half_cheetah_dt0.01_only_vel',
        epoch = 2000,
    )

    env = complex_agent_pos['environment']
    env.render_mode = 'rgb_array'

    with open(f'{inference_path}/variant.json', 'r') as file:
        variant = json.load(file)

    # ptu.set_gpu_mode(variant['util_params']['use_gpu'], variant['util_params']['gpu_id'])

    m = variant['algo_params']['sac_layer_size']
    simple_env = ENVS[variant['env_name']](**variant['env_params'])         # Just used for initilization purposes

    ### PARAMETERS ###
    obs_dim = int(np.prod(simple_env.observation_space.shape))
    action_dim = int(np.prod(simple_env.action_space.shape))
    net_complex_enc_dec = variant['reconstruction_params']['net_complex_enc_dec']
    latent_dim = variant['algo_params']['latent_size']
    time_steps = variant['algo_params']['time_steps']
    num_classes = variant['reconstruction_params']['num_classes']
    max_path_len = variant['algo_params']['max_path_length']
    reward_dim = 1
    encoder_input_dim = time_steps * (obs_dim + reward_dim + obs_dim)
    shared_dim = int(encoder_input_dim / time_steps * net_complex_enc_dec)
    if variant['algo_params']['sac_context_type']  == 'sample':
        policy_latent_dim = latent_dim
    else:
        policy_latent_dim  = latent_dim * 2

    
    bnp_model = BNPModel(
        save_dir=variant['dpmm_params']['save_dir'],
        start_epoch=variant['dpmm_params']['start_epoch'],
        gamma0=variant['dpmm_params']['gamma0'],
        num_lap=variant['dpmm_params']['num_lap'],
        fit_interval=variant['dpmm_params']['fit_interval'],
        kl_method=variant['dpmm_params']['kl_method'],
        birth_kwargs=variant['dpmm_params']['birth_kwargs'],
        merge_kwargs=variant['dpmm_params']['merge_kwargs']
    )

    memory = Memory(1e+6)
    encoder = get_encoder(inference_path, shared_dim, encoder_input_dim)
    simple_agent = get_simple_agent(inference_path, obs_dim, policy_latent_dim, action_dim, m)
    transfer_function_pos = get_complex_agent(env, complex_agent_pos)
    transfer_function_vel = get_complex_agent(env, complex_agent_vel)
    output_action_dim = 8
    decoder = SAC(n_states=20,
                n_actions=1,
                task_dim = 4,   # desired state
                hidden_layers_actor = [64,64,64],
                hidden_layers_critic = [64,64,64],
                memory_size=1e+6,
                batch_size=256,
                gamma=0.9,
                alpha=0.2,
                lr=3e-4,
                action_bounds=[-50,50],
                reward_scale=1)

    ### ROLLOUT ###
    # for i, task in enumerate(tasks):
    #     simple_env.reset_model()
    #     if i == 0:
    #         res = rollout(task, env, encoder, decoder, simple_agent, high_level_controller,
    #                                     transfer_function, variant, obs_dim, action_dim, 
    #                                     max_path_len, n_tasks=1, inner_loop_steps=10, save_video_path=inference_path)
    #         latent_vars = res[0]
    #         labels = res[1]
    #     else:
    #         res = rollout(task, env, encoder, decoder, simple_agent, high_level_controller, 
    #                                     transfer_function, variant, obs_dim, action_dim, 
    #                                     max_path_len, n_tasks=1, inner_loop_steps=10, save_video_path=inference_path)
    #         latent_vars = np.concatenate((latent_vars, res[0]), axis = 0)
    #         labels = np.concatenate((labels, res[1]), axis=0)
    rollout(env, encoder, decoder, simple_agent,
                                        transfer_function_pos, transfer_function_vel, memory, variant, obs_dim, action_dim, 
                                        max_path_len, n_tasks=1, inner_loop_steps=10, save_video_path=inference_path)
    ### Save metadata, tensors, videos
    # create_tsne(np.array(latent_vars), np.array(labels), inference_path)