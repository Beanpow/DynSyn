from typing import List, Tuple, Dict, Optional, Type, TypeVar

import torch as th
from torch import nn
from gymnasium import spaces

from stable_baselines3 import SAC
from stable_baselines3.sac.policies import SACPolicy, Actor, LOG_STD_MIN, LOG_STD_MAX
from stable_baselines3.common.preprocessing import get_action_dim
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor, create_mlp
from stable_baselines3.common.distributions import (
    SquashedDiagGaussianDistribution,
    StateDependentNoiseDistribution,
)

from dynsyn.algorithms.common.dynsynLayer import DynSynSACLayer
from dynsyn.algorithms.common.utils import get_dynsyn_weight_amp

SelfDynSynSAC = TypeVar("SelfDynSynSAC", bound="DynSynSAC")


class DynSynSACActor(Actor):
    def __init__(
        self,
        # DynSyn
        dynsyn: List[List[int]],
        dynsyn_log_std: float,
        # Original
        observation_space: spaces.Space,
        action_space: spaces.Box,
        net_arch: List[int],
        features_extractor: nn.Module,
        features_dim: int,
        activation_fn: Type[nn.Module] = nn.ReLU,
        use_sde: bool = False,
        log_std_init: float = -3,
        full_std: bool = True,
        use_expln: bool = False,
        clip_mean: float = 2.0,
        normalize_images: bool = True,
    ):
        super(Actor, self).__init__(
            observation_space,
            action_space,
            features_extractor=features_extractor,
            normalize_images=normalize_images,
            squash_output=True,
        )

        # Save arguments to re-create object at loading
        self.use_sde = use_sde
        self.sde_features_extractor = None
        self.net_arch = net_arch
        self.features_dim = features_dim
        self.activation_fn = activation_fn
        self.log_std_init = log_std_init
        self.use_expln = use_expln
        self.full_std = full_std
        self.clip_mean = clip_mean

        last_layer_dim = net_arch[-1] if len(net_arch) > 0 else features_dim
        self.dynsyn_layer = DynSynSACLayer(
            dynsyn, last_layer_dim=last_layer_dim, dynsyn_log_std=dynsyn_log_std
        )  # Add dynsyn Layer
        action_dim = get_action_dim(self.action_space) - (
            self.dynsyn_layer.muscle_dims - self.dynsyn_layer.muscle_group_num
        )
        latent_pi_net = create_mlp(features_dim, -1, net_arch, activation_fn)
        self.latent_pi = nn.Sequential(*latent_pi_net)

        if self.use_sde:
            self.action_dist = StateDependentNoiseDistribution(
                action_dim, full_std=full_std, use_expln=use_expln, learn_features=True, squash_output=True
            )
            self.mu, self.log_std = self.action_dist.proba_distribution_net(
                latent_dim=last_layer_dim, latent_sde_dim=last_layer_dim, log_std_init=log_std_init
            )
            # Avoid numerical issues by limiting the mean of the Gaussian
            # to be in [-clip_mean, clip_mean]
            if clip_mean > 0.0:
                self.mu = nn.Sequential(self.mu, nn.Hardtanh(min_val=-clip_mean, max_val=clip_mean))
        else:
            self.action_dist = SquashedDiagGaussianDistribution(action_dim)  # type: ignore[assignment]
            self.mu = nn.Linear(last_layer_dim, action_dim)
            self.log_std = nn.Linear(last_layer_dim, action_dim)  # type: ignore[assignment]

    def get_action_dist_params(
        self, obs: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor, Dict[str, th.Tensor], th.Tensor]:
        """
        Get the parameters for the action distribution.

        :param obs:
        :return:
            Mean, standard deviation and optional keyword arguments.
        """
        features = self.extract_features(obs, self.features_extractor)
        latent_pi = self.latent_pi(features)
        mean_actions = self.mu(latent_pi)

        if self.use_sde:
            return mean_actions, self.log_std, dict(latent_sde=latent_pi)
        # Unstructured exploration (Original implementation)
        log_std = self.log_std(latent_pi)  # type: ignore[operator]
        # Original Implementation to cap the standard deviation
        log_std = th.clamp(log_std, LOG_STD_MIN, LOG_STD_MAX)
        return mean_actions, log_std, {}, latent_pi

    def forward(self, obs: th.Tensor, deterministic: bool = False) -> th.Tensor:
        mean_actions, log_std, kwargs, latent_pi = self.get_action_dist_params(obs)
        # Note: the action is squashed
        action = self.action_dist.actions_from_params(
            mean_actions, log_std, deterministic=deterministic, **kwargs
        )
        action = self.dynsyn_layer(action, latent_pi, deterministic=deterministic)
        return action

    def action_log_prob(self, obs: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        mean_actions, log_std, kwargs, latent_pi = self.get_action_dist_params(obs)
        # return action and associated log prob
        mean_actions, log_std = self.action_dist.log_prob_from_params(mean_actions, log_std, **kwargs)
        action = self.dynsyn_layer(mean_actions, latent_pi)

        return action, log_std


class DynSynSACPolicy(SACPolicy):
    def __init__(self, *args, dynsyn: List[List[int]], dynsyn_log_std: float, **kwargs):
        self.dynsyn = dynsyn
        self.dynsyn_log_std = dynsyn_log_std
        super().__init__(*args, **kwargs)

    def make_actor(self, features_extractor: Optional[BaseFeaturesExtractor] = None) -> Actor:
        self.actor_kwargs.update({"dynsyn": self.dynsyn, "dynsyn_log_std": self.dynsyn_log_std})
        actor_kwargs = self._update_features_extractor(self.actor_kwargs, features_extractor)
        return DynSynSACActor(**actor_kwargs).to(self.device)


class DynSynSAC(SAC):
    def __init__(
        self,
        *args,
        dynsyn_k: float = 0,
        dynsyn_a: float = 0,
        dynsyn_weight_amp: Optional[float] = None,
        **kwargs
    ):
        self.dynsyn_k = dynsyn_k
        self.dynsyn_a = dynsyn_a
        self.dynsyn_weight_amp = dynsyn_weight_amp

        super().__init__(*args, **kwargs)

    def _setup_model(self) -> None:
        super()._setup_model()
        self.actor.dynsyn_layer.update_dynsyn_weight_amp(self.dynsyn_weight_amp)

    def train(self, gradient_steps: int, batch_size: int = 64) -> None:
        dynsyn_weight_amp = get_dynsyn_weight_amp(
            self.num_timesteps, self.dynsyn_k, self.dynsyn_a, self.dynsyn_weight_amp
        )
        self.actor.dynsyn_layer.update_dynsyn_weight_amp(dynsyn_weight_amp=dynsyn_weight_amp)

        super().train(gradient_steps=gradient_steps, batch_size=batch_size)
        self.logger.record("train/dynsyn_weight_amp", dynsyn_weight_amp)

    def learn(self, *args, **kwargs) -> SelfDynSynSAC:
        dynsyn_weight_amp = get_dynsyn_weight_amp(0, self.dynsyn_k, self.dynsyn_a, self.dynsyn_weight_amp)
        self.actor.dynsyn_layer.update_dynsyn_weight_amp(dynsyn_weight_amp)
        return super().learn(*args, **kwargs)
