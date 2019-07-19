import torch
import torch.nn
import numpy as np 
import torch.nn.functional as F 
import torch.multiprocessing as mp
import torch.optim as optim
from torch.distributions import Categorical
import gym
import os


class Model(nn.Module):

    def __init__(self, input_dim, output_dim):
        super(Model, self).__init__()
        self.policy1 = nn.Linear(input_dim, 256) 
        self.policy2 = nn.Linear(256, output_dim)

        self.value1 = nn.Linear(input_dim, 256)
        self.value2 = nn.Linear(256, 1)
        
    def forward(self, state):
        x = F.relu(self.policy1(state))
        logits = self.policy2(x)

        v = F.relu(self.value1(state))
        v = self.value2(v)

        return logits, values
        

# single actor-critic agent
class Worker(mp.Process):

    def __init__(self, id, env_id, gamma, global_network, global_optimizer, global_episode, GLOBAL_MAX_EPISODE):
        super(Worker, self).__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.id = "w%i" % id
        self.env = gym.make(env_id)
        self.obs_dim = env.observation_space.shape[0]
        self.action_dim = env.action_space.n

        self.gamma = gamma
        self.local_network = Model(self.obs_dim, self.action_dim) 
        self.local_episodes = 0

        self.global_network = global_network
        self.global_episode = global_episode
        self.global_optimizer = global_optimizer
        self.GLOBAL_MAX_EPISODE = GLOBAL_MAX_EPISODE
    
    def get_action(self, state):
        state = torch.FloatTensor(state).to(self.device)
        logits, _ = self.model.forward(state)
        dist = F.softmax(logits, dim=0)
        probs = Categorical(dist)

        return probs.sample().cpu().detach().item()
    
    def compute_loss(self, trajectory):
        states = torch.FloatTensor([sars[0] for sars in trajectory]).to(self.device)
        actions = torch.LongTensor([sars[1] for sars in trajectory]).view(-1, 1).to(self.device)
        rewards = torch.FloatTensor([sars[2] for sars in trajectory]).to(self.device)
        next_states = torch.FloatTensor([sars[3] for sars in trajectory]).to(self.device)
        dones = torch.FloatTensor([sars[4] for sars in trajectory]).view(-1, 1).to(self.device)
        
        # compute discounted rewards
        discounted_rewards = [torch.sum(torch.FloatTensor([self.gamma**i for i in range(rewards[j:].size(0))])\
             * rewards[j:]) for j in range(rewards.size(0))]  # sorry, not the most readable code.
        
        logits, values = self.local_network.forward(states)
        dists = F.softmax(logits, dim=1)
        probs = Categorical(dists)
        
        # compute value loss
        value_targets = rewards.view(-1, 1) + torch.FloatTensor(discounted_rewards).view(-1, 1).to(self.device)
        value_loss = F.mse_loss(values, value_targets.detach())
        
        # compute entropy bonus
        entropy = []
        for dist in dists:
            entropy.append(-torch.sum(dist.mean() * torch.log(dist)))
        entropy = torch.stack(entropy).sum()
        
        # compute policy loss
        advantage = value_targets - values
        policy_loss = -probs.log_prob(actions.view(actions.size(0))).view(-1, 1) * advantage.detach()
        policy_loss = policy_loss.mean()
        
        total_loss = policy_loss + value_loss - 0.001 * entropy 
        return total_loss

    def update_global(self, trajectory):
        loss = self.compute_loss(trajectory)
        
        self.global_optimizer.zero_grad()
        loss.backward()
        # propagate local gradients to global parameters
        for local_params, global_params in zip(self.local_network.parameters(), self.global_network.parameters()):
            global_params._grad = local_params._grad
        # update global
        self.global_optimizer.step()

    def sync_with_global(self):
        for local_params, global_params in zip(self.local_network.parameters(), self.global_network.parameters()):
            local_params.copy_(global_params)

    def run(self):
        state = self.env.reset()
        trajectory = [] # [[s, a, r, s', done], [], ...]
        episode_reward = 0
        
        while self.local_episodes < self.GLOBAL_MAX_EPISODE:
            action = self.get_action(state)
            next_state, reward, done, _ = self.env.step(action)
            trajectory.append([state, action, reward, next_state, done])
            episode_reward += reward

            if done:
                self.local_episodes += 1
                update_global(trajectory)
                sync_with_global(global_network)

                trajectory = []
                episode_reward = 0
                state = self.env.reset()
            
            state = next_state

class SharedAdam(torch.optim.Adam):
    def __init__(self, params, lr=1e-3, betas=(0.9, 0.9), eps=1e-8, weight_decay=0):
        super(SharedAdam, self).__init__(params, lr=lr, betas=betas, eps=eps, weight_decay=weight_decay).__init__()
        # state initialization
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                state['step'] = 0
                state['exp_avg'] = torch.zeros_like(p.data)
                state['exp_avg_sq'] = torch.zeros_like(p.data)

                # share in memory
                state['exp_avg'].share_memory_()
                state['exp_avg_sq'].share_memory_()

if __name__ == "__main__":
    env = gym.make("CartPole-v0")
    global_net = Model(env.observation_space.shape[0], env.action_space.n)
    global_opt = SharedAdam(global_net.parameters())
    global_episode = mp.Value('i', 0)

    workers = [Worker(i, "CartPole-v0", 0.99, global_net, global_opt, global_episode, GLOBAL_MAX_EPISODE)]
