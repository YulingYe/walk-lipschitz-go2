import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
from params_proto import PrefixProto

from go2_gym_learn.ppo_cse import ActorCritic_Lips
from go2_gym_learn.ppo_cse import Discriminator
from go2_gym_learn.ppo_cse import ReplayBuffer
from go2_gym_learn.ppo_cse import RolloutStorage
from go2_gym_learn.ppo_cse import caches


class LIPS_Args(PrefixProto):
    # algorithm
    value_loss_coef = 1.0
    use_clipped_value_loss = True
    clip_param = 0.2
    entropy_coef = 0.01
    num_learning_epochs = 5
    num_mini_batches = 4  # mini batch size = num_envs*nsteps / nminibatches
    learning_rate = 1.e-3  # 5.e-4
    adaptation_module_learning_rate = 1.e-3
    num_adaptation_module_substeps = 1
    schedule = 'adaptive'  # could be adaptive, fixed
    gamma = 0.99
    lam = 0.95
    desired_kl = 0.01
    max_grad_norm = 1.

    selective_adaptation_module_loss = False


class LIPS:
    actor_critic: ActorCritic_Lips
    discriminator: Discriminator

    def __init__(
            self, 
            actor_critic, 
            discriminator,
            wasabi_expert_data,
            wasabi_state_normalizer,
            wasabi_style_reward_normalizer,

            device='cpu',
            discriminator_learning_rate=0.000025,
            discriminator_momentum=0.9,
            discriminator_weight_decay=0.0005,
            discriminator_gradient_penalty_coef=5,
            discriminator_loss_function="MSELoss", # MSELoss
            discriminator_num_mini_batches=10,
            wasabi_replay_buffer_size=100000,
        ):

        self.device = device
        # PPO components
        self.actor_critic = actor_critic
        self.actor_critic.to(device)
        self.storage = None  # initialized later
        self.optimizer = optim.Adam(self.actor_critic.parameters(), lr=LIPS_Args.learning_rate)
        self.adaptation_module_optimizer = optim.Adam(self.actor_critic.parameters(),
                                                      lr=LIPS_Args.adaptation_module_learning_rate)
        if self.actor_critic.decoder:
            self.decoder_optimizer = optim.Adam(self.actor_critic.parameters(),
                                                          lr=LIPS_Args.adaptation_module_learning_rate)
        self.transition = RolloutStorage.Transition()

        self.learning_rate = LIPS_Args.learning_rate

        # Discriminator components
        self.discriminator = discriminator
        self.discriminator.to(self.device)
        self.wasabi_policy_data = ReplayBuffer(discriminator.observation_dim, discriminator.observation_horizon, wasabi_replay_buffer_size, device) #wasabi策略数据
        self.wasabi_expert_data = wasabi_expert_data #wasabi参考数据
        self.wasabi_state_normalizer = wasabi_state_normalizer #wasabi状态规范化
        self.wasabi_style_reward_normalizer = wasabi_style_reward_normalizer #wasabi风格规范化

        # Discriminator parameters
        self.discriminator_learning_rate = discriminator_learning_rate
        self.discriminator_momentum = discriminator_momentum
        self.discriminator_weight_decay = discriminator_weight_decay
        self.discriminator_gradient_penalty_coef = discriminator_gradient_penalty_coef
        self.discriminator_loss_function = discriminator_loss_function
        self.discriminator_num_mini_batches = discriminator_num_mini_batches

        if self.discriminator_loss_function == "WassersteinLoss":
            discriminator_optimizer = optim.RMSprop
        else:
            discriminator_optimizer = optim.SGD
        self.discriminator_optimizer = discriminator_optimizer(
                                                    self.discriminator.parameters(),
                                                    lr=self.discriminator_learning_rate,
                                                    momentum=self.discriminator_momentum,
                                                    weight_decay=self.discriminator_weight_decay,
                                                )


    def init_storage(self, num_envs, num_transitions_per_env, actor_obs_shape, privileged_obs_shape, obs_history_shape,
                     action_shape):
        self.storage = RolloutStorage(num_envs, num_transitions_per_env, actor_obs_shape, privileged_obs_shape,
                                      obs_history_shape, action_shape, self.device)

    def test_mode(self):
        self.actor_critic.test()

    def train_mode(self):
        self.actor_critic.train()

    def act(self, obs, privileged_obs, obs_history, wasabi_observation_buf):
        # Compute the actions and values
        self.transition.actions = self.actor_critic.act(obs_history).detach()
        self.transition.values = self.actor_critic.evaluate(obs_history, privileged_obs).detach()
        self.transition.actions_log_prob = self.actor_critic.get_actions_log_prob(self.transition.actions).detach()
        self.transition.action_mean = self.actor_critic.action_mean.detach()
        self.transition.action_sigma = self.actor_critic.action_std.detach()
        # need to record obs and critic_obs before env.step()
        self.transition.observations = obs
        self.transition.critic_observations = obs
        self.transition.privileged_observations = privileged_obs
        self.transition.observation_histories = obs_history
        self.wasabi_observation_buf = wasabi_observation_buf.clone()
        return self.transition.actions

    def process_env_step(self, rewards, dones, infos, wasabi_obs):
        self.transition.rewards = rewards.clone()
        self.transition.dones = dones
        self.transition.env_bins = infos["env_bins"]
        # Bootstrapping on time outs
        if 'time_outs' in infos:
            self.transition.rewards += LIPS_Args.gamma * torch.squeeze(
                self.transition.values * infos['time_outs'].unsqueeze(1).to(self.device), 1)

        # Record the transition
        self.storage.add_transitions(self.transition)
        wasabi_observation_buf = torch.cat((self.wasabi_observation_buf[:, 1:], wasabi_obs.unsqueeze(1)), dim=1)
        self.wasabi_policy_data.insert(wasabi_observation_buf)
        self.transition.clear()
        self.actor_critic.reset(dones)

    def compute_returns(self, last_critic_obs, last_critic_privileged_obs):
        last_values = self.actor_critic.evaluate(last_critic_obs, last_critic_privileged_obs).detach()
        self.storage.compute_returns(last_values, LIPS_Args.gamma, LIPS_Args.lam)

    def update(self):
        mean_value_loss = 0
        mean_surrogate_loss = 0
        mean_adaptation_module_loss = 0
        mean_decoder_loss = 0
        mean_decoder_loss_student = 0
        mean_adaptation_module_test_loss = 0
        mean_decoder_test_loss = 0
        mean_decoder_test_loss_student = 0
        mean_wasabi_loss = 0
        mean_grad_pen_loss = 0
        mean_policy_pred = 0
        mean_expert_pred = 0

        generator = self.storage.mini_batch_generator(LIPS_Args.num_mini_batches, LIPS_Args.num_learning_epochs)
        for obs_batch, critic_obs_batch, privileged_obs_batch, obs_history_batch, actions_batch, target_values_batch, advantages_batch, returns_batch, old_actions_log_prob_batch, \
            old_mu_batch, old_sigma_batch, masks_batch, env_bins_batch in generator:

            self.actor_critic.act(obs_history_batch, masks=masks_batch)
            actions_log_prob_batch = self.actor_critic.get_actions_log_prob(actions_batch)
            value_batch = self.actor_critic.evaluate(obs_history_batch, privileged_obs_batch, masks=masks_batch)
            mu_batch = self.actor_critic.action_mean
            sigma_batch = self.actor_critic.action_std
            entropy_batch = self.actor_critic.entropy

            # KL
            if LIPS_Args.desired_kl != None and LIPS_Args.schedule == 'adaptive':
                with torch.inference_mode():
                    kl = torch.sum(
                        torch.log(sigma_batch / old_sigma_batch + 1.e-5) + (
                                torch.square(old_sigma_batch) + torch.square(old_mu_batch - mu_batch)) / (
                                2.0 * torch.square(sigma_batch)) - 0.5, axis=-1)
                    kl_mean = torch.mean(kl)

                    if kl_mean > LIPS_Args.desired_kl * 2.0:
                        self.learning_rate = max(1e-5, self.learning_rate / 1.5)
                    elif kl_mean < LIPS_Args.desired_kl / 2.0 and kl_mean > 0.0:
                        self.learning_rate = min(1e-2, self.learning_rate * 1.5)

                    for param_group in self.optimizer.param_groups:
                        param_group['lr'] = self.learning_rate

            # Surrogate loss
            ratio = torch.exp(actions_log_prob_batch - torch.squeeze(old_actions_log_prob_batch))
            surrogate = -torch.squeeze(advantages_batch) * ratio
            surrogate_clipped = -torch.squeeze(advantages_batch) * torch.clamp(ratio, 1.0 - LIPS_Args.clip_param,
                                                                               1.0 + LIPS_Args.clip_param)
            surrogate_loss = torch.max(surrogate, surrogate_clipped).mean()

            # Value function loss
            if LIPS_Args.use_clipped_value_loss:
                value_clipped = target_values_batch + \
                                (value_batch - target_values_batch).clamp(-LIPS_Args.clip_param,
                                                                          LIPS_Args.clip_param)
                value_losses = (value_batch - returns_batch).pow(2)
                value_losses_clipped = (value_clipped - returns_batch).pow(2)
                value_loss = torch.max(value_losses, value_losses_clipped).mean()
            else:
                value_loss = (returns_batch - value_batch).pow(2).mean()

            loss = surrogate_loss + LIPS_Args.value_loss_coef * value_loss - LIPS_Args.entropy_coef * entropy_batch.mean()

            # Gradient step
            self.optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(self.actor_critic.parameters(), LIPS_Args.max_grad_norm)
            self.optimizer.step()

            mean_value_loss += value_loss.item()
            mean_surrogate_loss += surrogate_loss.item()

            data_size = privileged_obs_batch.shape[0]
            num_train = int(data_size // 5 * 4)

            # Adaptation module gradient step

            for epoch in range(LIPS_Args.num_adaptation_module_substeps):

                adaptation_pred = self.actor_critic.adaptation_module(obs_history_batch)
                with torch.no_grad():
                    adaptation_target = privileged_obs_batch
                    # residual = (adaptation_target - adaptation_pred).norm(dim=1)
                    # caches.slot_cache.log(env_bins_batch[:, 0].cpu().numpy().astype(np.uint8),
                    #                       sysid_residual=residual.cpu().numpy())

                selection_indices = torch.linspace(0, adaptation_pred.shape[1]-1, steps=adaptation_pred.shape[1], dtype=torch.long)
                if LIPS_Args.selective_adaptation_module_loss:
                    # mask out indices corresponding to swing feet
                    selection_indices = 0

                adaptation_loss = F.mse_loss(adaptation_pred[:num_train, selection_indices], adaptation_target[:num_train, selection_indices])
                adaptation_test_loss = F.mse_loss(adaptation_pred[num_train:, selection_indices], adaptation_target[num_train:, selection_indices])



                self.adaptation_module_optimizer.zero_grad()
                adaptation_loss.backward()
                self.adaptation_module_optimizer.step()

                mean_adaptation_module_loss += adaptation_loss.item()
                mean_adaptation_module_test_loss += adaptation_test_loss.item()
        
        # Discriminator update
        wasabi_policy_generator = self.wasabi_policy_data.feed_forward_generator(
            self.discriminator_num_mini_batches,
            self.storage.num_envs * self.storage.num_transitions_per_env // self.discriminator_num_mini_batches) #策略数据生成
        wasabi_expert_generator = self.wasabi_expert_data.feed_forward_generator(
            self.discriminator_num_mini_batches,
            self.storage.num_envs * self.storage.num_transitions_per_env // self.discriminator_num_mini_batches) #专家数据生成

        for sample_wasabi_policy, sample_wasabi_expert in zip(wasabi_policy_generator, wasabi_expert_generator):

            # Discriminator loss
            policy_state_buf = torch.zeros_like(sample_wasabi_policy)
            expert_state_buf = torch.zeros_like(sample_wasabi_expert)
            if self.wasabi_state_normalizer is not None:
                for i in range(self.discriminator.observation_horizon):
                    with torch.no_grad():
                        policy_state_buf[:, i] = self.wasabi_state_normalizer.normalize(sample_wasabi_policy[:, i])
                        expert_state_buf[:, i] = self.wasabi_state_normalizer.normalize(sample_wasabi_expert[:, i])
            policy_d = self.discriminator(policy_state_buf.flatten(1, 2))
            expert_d = self.discriminator(expert_state_buf.flatten(1, 2))
            # 判别器损失函数选择
            if self.discriminator_loss_function == "BCEWithLogitsLoss":
                expert_loss = torch.nn.BCEWithLogitsLoss()(expert_d, torch.ones_like(expert_d))
                policy_loss = torch.nn.BCEWithLogitsLoss()(policy_d, torch.zeros_like(policy_d))
            elif self.discriminator_loss_function == "MSELoss":
                expert_loss = torch.nn.MSELoss()(expert_d, torch.ones(expert_d.size(), device=self.device))
                policy_loss = torch.nn.MSELoss()(policy_d, -1 * torch.ones(policy_d.size(), device=self.device))
            elif self.discriminator_loss_function == "WassersteinLoss":
                expert_loss = -expert_d.mean()
                policy_loss = policy_d.mean()
            else:
                raise ValueError("Unexpected loss function specified")
            wasabi_loss = 0.5 * (expert_loss + policy_loss)
            grad_pen_loss = self.discriminator.compute_grad_pen(sample_wasabi_expert,
                                                                lambda_=self.discriminator_gradient_penalty_coef) #计算技能判别梯度

            # Gradient step
            discriminator_loss = wasabi_loss + grad_pen_loss
            self.discriminator_optimizer.zero_grad()
            discriminator_loss.backward()
            self.discriminator_optimizer.step()

            if self.wasabi_state_normalizer is not None:
                self.wasabi_state_normalizer.update(sample_wasabi_policy[:, 0])
                self.wasabi_state_normalizer.update(sample_wasabi_expert[:, 0])

            mean_wasabi_loss += wasabi_loss.item()
            mean_grad_pen_loss += grad_pen_loss.item()
            mean_policy_pred += policy_d.mean().item()
            mean_expert_pred += expert_d.mean().item()


        num_updates = LIPS_Args.num_learning_epochs * LIPS_Args.num_mini_batches
        mean_value_loss /= num_updates
        mean_surrogate_loss /= num_updates
        mean_adaptation_module_loss /= (num_updates * LIPS_Args.num_adaptation_module_substeps)
        mean_decoder_loss /= (num_updates * LIPS_Args.num_adaptation_module_substeps)
        mean_decoder_loss_student /= (num_updates * LIPS_Args.num_adaptation_module_substeps)
        mean_adaptation_module_test_loss /= (num_updates * LIPS_Args.num_adaptation_module_substeps)
        mean_decoder_test_loss /= (num_updates * LIPS_Args.num_adaptation_module_substeps)
        mean_decoder_test_loss_student /= (num_updates * LIPS_Args.num_adaptation_module_substeps)

        discriminator_num_updates = self.discriminator_num_mini_batches
        mean_wasabi_loss /= discriminator_num_updates
        mean_grad_pen_loss /= discriminator_num_updates
        mean_policy_pred /= discriminator_num_updates
        mean_expert_pred /= discriminator_num_updates

        self.storage.clear()

        return mean_value_loss, mean_surrogate_loss, mean_adaptation_module_loss, mean_decoder_loss, mean_decoder_loss_student, mean_adaptation_module_test_loss, mean_decoder_test_loss, mean_decoder_test_loss_student, \
               mean_wasabi_loss, mean_grad_pen_loss, mean_policy_pred, mean_expert_pred
