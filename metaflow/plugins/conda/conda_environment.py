import json
import os
import sys
import tarfile

from metaflow.metaflow_environment import MetaflowEnvironment
from metaflow.exception import MetaflowException
from metaflow.mflog import BASH_SAVE_LOGS

from .conda import Conda
from . import get_conda_manifest_path, CONDA_MAGIC_FILE


class CondaEnvironment(MetaflowEnvironment):
    TYPE = 'conda'
    _filecache = None

    def __init__(self, flow):
        self.flow = flow
        self.local_root = None
        # A conda environment sits on top of whatever default environment
        # the user has so we get that environment to be able to forward
        # any calls we don't handle specifically to that one.
        from ...plugins import ENVIRONMENTS
        from metaflow.metaflow_config import DEFAULT_ENVIRONMENT
        self.base_env = [e for e in ENVIRONMENTS + [MetaflowEnvironment]
            if e.TYPE == DEFAULT_ENVIRONMENT][0](self.flow)

    def init_environment(self, echo):
        # Print a message for now
        echo("Bootstrapping conda environment..." +
            "(this could take a few minutes)")
        self.base_env.init_environment(echo)

    def validate_environment(self, echo):
        return self.base_env.validate_environment(echo)

    def decospecs(self):
        # Apply conda decorator to all steps
        return ('conda', )

    def _get_conda_decorator(self, step_name):
        step = next(step for step in self.flow if step.name == step_name)
        decorator = next(deco for deco in step.decorators if deco.name == 'conda')
        # Guaranteed to have a conda decorator because of self.decospecs()
        return decorator

    def _get_env_id(self, step_name):
        conda_decorator = self._get_conda_decorator(step_name)
        if conda_decorator.is_enabled():
            return conda_decorator._env_id()
        return None

    def _get_executable(self, step_name):
        env_id = self._get_env_id(step_name)
        if env_id is not None:
            return (os.path.join(env_id, "bin/python -s"))
        return None

    def set_local_root(self, ds_root):
        self.local_root = ds_root

    def bootstrap_commands(self, step_name):
        # Bootstrap conda and execution environment for step
        env_id = self._get_env_id(step_name)
        if env_id is not None:
            return [
                    "echo \'Bootstrapping environment...\'",
                    "python -m metaflow.plugins.conda.batch_bootstrap \"%s\" %s" % \
                        (self.flow.name, env_id),
                    "echo \'Environment bootstrapped.\'",
                ]
        return []

    def add_to_package(self):
        files = self.base_env.add_to_package()
        # Add conda manifest file to job package at the top level.
        path = get_conda_manifest_path(self.local_root, self.flow.name)
        if os.path.exists(path):
            files.append((path, os.path.basename(path)))
        return files

    def pylint_config(self):
        config = self.base_env.pylint_config()
        # Disable (import-error) in pylint
        config.append('--disable=F0401')
        return config

    def executable(self, step_name):
        # Get relevant python interpreter for step
        executable = self._get_executable(step_name)
        if executable is not None:
            return executable
        return self.base_env.executable(step_name)

    @classmethod
    def get_info(cls, flow_name, metadata):
        if cls._filecache is None:
            from metaflow.client.filecache import FileCache
            cls._filecache = FileCache()
        info = metadata.get('code-package')
        env_id = metadata.get('conda_env_id')
        if info is None or env_id is None:
            return {'type': CondaEnvironment.TYPE}
        info = json.loads(info)
        with cls._filecache.get_data(info['ds_type'], flow_name, info['sha']) as f:
            tar = tarfile.TarFile(fileobj=f)
            conda_file = tar.extractfile(CONDA_MAGIC_FILE)
            if conda_file is None:
                return {'type': CondaEnvironment.TYPE}
            info = json.loads(conda_file.read().decode('utf-8'))
        # TODO: Provide a better schema for exposing explicit dependencies
        # and transitive dependencies in the returned dict. The current flat
        # structure may not work as we introduce more environment types.
        new_info = {
            'type': CondaEnvironment.TYPE,
            'explicit': info[env_id]['explicit'],
            'deps': info[env_id]['deps']}
        return new_info

    def get_package_commands(self, code_package_url):
        return self.base_env.get_package_commands(code_package_url)

    def get_environment_info(self):
        return self.base_env.get_environment_info()
