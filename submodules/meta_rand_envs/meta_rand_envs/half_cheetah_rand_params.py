import numpy as np
from meta_rand_envs.base import RandomEnv
from gym import utils
from gym.spaces import Box


class HalfCheetahRandParamsEnv(RandomEnv, utils.EzPickle):
    def __init__(self, log_scale_limit=3.0, mode="gentle", change_prob=0.01):
        self.change_prob = change_prob
        self.changed = False
        self.steps = 0
        observation_space = Box(low=-np.inf, high=np.inf, shape=(20,), dtype=np.float64)
        RandomEnv.__init__(self, log_scale_limit, 'half_cheetah.xml', 5,  observation_space, hfield_mode=mode, rand_params=['body_mass', 'dof_damping', 'body_inertia', 'geom_friction'])
        utils.EzPickle.__init__(self)

        self._init_geom_rgba = self.model.geom_rgba.copy()

    def _step(self, action):
        # with some probability change parameters
        prob = np.random.uniform(0, 1)
        if prob < self.change_prob and self.steps > 100 and not self.initialize and not self.changed:
            self.change_parameters()
            
        xposbefore = self.sim.data.qpos[0]
        self.do_simulation(action, self.frame_skip)
        xposafter = self.sim.data.qpos[0]
        ob = self._get_obs()
        reward_ctrl = - 0.1 * np.square(action).sum()
        reward_run = (xposafter - xposbefore) / self.dt
        reward = reward_ctrl + reward_run
        done = False
        self.steps += 1
        return ob, reward, done, dict(reward_run=reward_run, reward_ctrl=reward_ctrl)

    # special here: no velocity observed
    def _get_obs(self):
        return self.sim.data.qpos.flat[1:]

    def reset_model(self):
        # change related
        self.change_parameters_reset()
        
        # standard
        qpos = self.init_qpos + self.np_random.uniform(low=-.1, high=.1, size=self.model.nq)
        qvel = self.init_qvel + self.np_random.randn(self.model.nv) * .1
        self.set_state(qpos, qvel)
        return self._get_obs()

    def viewer_setup(self):
        self.viewer.cam.distance = self.model.stat.extent * 0.5
        
    def change_parameters(self):
        # intermediate change log_scale_limit
        temp_log_scale_limit = self.log_scale_limit
        self.log_scale_limit = 3
        new_params = self.sample_tasks(1)
        self.set_physical_parameters(new_params[0])
        self.log_scale_limit = temp_log_scale_limit
        # recolor
        geom_rgba = self._init_geom_rgba.copy()
        geom_rgba[1:, :3] = np.array([1, 0, 0])
        self.model.geom_rgba[:] = geom_rgba
        self.changed = True

    def change_parameters_reset(self):
        # reset changes
        self.changed = False
        self.steps = 0
        self.set_physical_parameters(self._task)

        self.model.geom_rgba[:] = self._init_geom_rgba.copy()


if __name__ == "__main__":

    env = HalfCheetahHfieldRandParamsEnv()
    tasks = env.sample_tasks(40)
    while True:
        env.reset()
        env.set_task(np.random.choice(tasks))
        print(env.model.body_mass)
        for _ in range(2000):
            env.render()
            env.step(env.action_space.sample())  # take a random action

