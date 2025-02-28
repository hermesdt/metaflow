import click
import os
import sys
import time
import traceback

from metaflow import util
from metaflow.exception import CommandException, METAFLOW_EXIT_DISALLOW_RETRY
from metaflow.metadata.util import sync_local_metadata_from_datastore
from metaflow.metaflow_config import DATASTORE_LOCAL_DIR
from metaflow.mflog import TASK_LOG_SOURCE
from metaflow.parameters import JSONTypeClass

from .kubernetes import Kubernetes, KubernetesKilledException

# TODO(s):
#    1. Compatibility for Metaflow-R (not a blocker for release).
#    2. Add more CLI commands to manage Kubernetes objects.


@click.group()
def cli():
    pass


@cli.group(help="Commands related to Kubernetes on Amazon EKS.")
def kubernetes():
    pass


@kubernetes.command(
    help="Execute a single task on Kubernetes using Amazon EKS. This command "
    "calls the top-level step command inside a Kubernetes job with the given "
    "options. Typically you do not call this command directly; it is used "
    "internally by Metaflow."
)
@click.argument("step-name")
@click.argument("code-package-sha")
@click.argument("code-package-url")
@click.option(
    "--executable",
    help="Executable requirement for Kubernetes job on Amazon EKS.",
)
@click.option(
    "--image", help="Docker image requirement for Kubernetes job on Amazon EKS."
)
@click.option(
    "--service-account",
    help="IRSA requirement for Kubernetes job on Amazon EKS.",
)
@click.option(
    "--secrets",
    multiple=True,
    default=None,
    help="Secrets for Kubernetes job on Amazon EKS.",
)
@click.option(
    "--node-selector",
    multiple=True,
    default=None,
    help="NodeSelector for Kubernetes job on Amazon EKS.",
)
@click.option(
    # Note that ideally we would have liked to use `namespace` rather than
    # `k8s-namespace` but unfortunately, `namespace` is already reserved for
    # Metaflow namespaces.
    "--k8s-namespace",
    default=None,
    help="Namespace for Kubernetes job on Amazon EKS.",
)
@click.option("--cpu", help="CPU requirement for Kubernetes job on Amazon EKS.")
@click.option("--gpu", help="GPU requirement for Kubernetes job on Amazon EKS.")
@click.option("--disk", help="Disk requirement for Kubernetes job on Amazon EKS.")
@click.option("--memory", help="Memory requirement for Kubernetes job on Amazon EKS.")
@click.option("--run-id", help="Passed to the top-level 'step'.")
@click.option("--task-id", help="Passed to the top-level 'step'.")
@click.option("--input-paths", help="Passed to the top-level 'step'.")
@click.option("--split-index", help="Passed to the top-level 'step'.")
@click.option("--clone-path", help="Passed to the top-level 'step'.")
@click.option("--clone-run-id", help="Passed to the top-level 'step'.")
@click.option(
    "--tag", multiple=True, default=None, help="Passed to the top-level 'step'."
)
@click.option("--namespace", default=None, help="Passed to the top-level 'step'.")
@click.option("--retry-count", default=0, help="Passed to the top-level 'step'.")
@click.option(
    "--max-user-code-retries", default=0, help="Passed to the top-level 'step'."
)
@click.option(
    "--run-time-limit",
    default=5 * 24 * 60 * 60,  # Default is set to 5 days
    help="Run time limit in seconds for Kubernetes job.",
)
@click.option(
    "--tolerations",
    default=None,
    type=JSONTypeClass(),
    multiple=True,
)
@click.pass_context
def step(
    ctx,
    step_name,
    code_package_sha,
    code_package_url,
    executable=None,
    image=None,
    service_account=None,
    secrets=None,
    node_selector=None,
    k8s_namespace=None,
    cpu=None,
    gpu=None,
    disk=None,
    memory=None,
    run_time_limit=None,
    tolerations=None,
    **kwargs
):
    def echo(msg, stream="stderr", job_id=None):
        msg = util.to_unicode(msg)
        if job_id:
            msg = "[%s] %s" % (job_id, msg)
        ctx.obj.echo_always(msg, err=(stream == sys.stderr))

    node = ctx.obj.graph[step_name]

    # Construct entrypoint CLI
    if executable is None:
        executable = ctx.obj.environment.executable(step_name)

    # Set environment
    env = {}
    env_deco = [deco for deco in node.decorators if deco.name == "environment"]
    if env_deco:
        env = env_deco[0].attributes["vars"]

    # Set input paths.
    input_paths = kwargs.get("input_paths")
    split_vars = None
    if input_paths:
        max_size = 30 * 1024
        split_vars = {
            "METAFLOW_INPUT_PATHS_%d" % (i // max_size): input_paths[i : i + max_size]
            for i in range(0, len(input_paths), max_size)
        }
        kwargs["input_paths"] = "".join("${%s}" % s for s in split_vars.keys())
        env.update(split_vars)

    # Set retry policy.
    retry_count = int(kwargs.get("retry_count", 0))
    retry_deco = [deco for deco in node.decorators if deco.name == "retry"]
    minutes_between_retries = None
    if retry_deco:
        minutes_between_retries = int(
            retry_deco[0].attributes.get("minutes_between_retries", 2)
        )
    if retry_count:
        ctx.obj.echo_always(
            "Sleeping %d minutes before the next retry" % minutes_between_retries
        )
        time.sleep(minutes_between_retries * 60)

    step_cli = u"{entrypoint} {top_args} step {step} {step_args}".format(
        entrypoint="%s -u %s" % (executable, os.path.basename(sys.argv[0])),
        top_args=" ".join(util.dict_to_cli_options(ctx.parent.parent.params)),
        step=step_name,
        step_args=" ".join(util.dict_to_cli_options(kwargs)),
    )

    # this information is needed for log tailing
    ds = ctx.obj.flow_datastore.get_task_datastore(
        mode="w",
        run_id=kwargs["run_id"],
        step_name=step_name,
        task_id=kwargs["task_id"],
        attempt=int(retry_count),
    )
    stdout_location = ds.get_log_location(TASK_LOG_SOURCE, "stdout")
    stderr_location = ds.get_log_location(TASK_LOG_SOURCE, "stderr")

    def _sync_metadata():
        if ctx.obj.metadata.TYPE == "local":
            sync_local_metadata_from_datastore(
                DATASTORE_LOCAL_DIR,
                ctx.obj.flow_datastore.get_task_datastore(
                    kwargs["run_id"], step_name, kwargs["task_id"]
                ),
            )

    try:
        kubernetes = Kubernetes(
            datastore=ctx.obj.flow_datastore,
            metadata=ctx.obj.metadata,
            environment=ctx.obj.environment,
            flow_name=ctx.obj.flow.name,
            run_id=kwargs["run_id"],
            step_name=step_name,
            task_id=kwargs["task_id"],
            attempt=retry_count,
        )
        # Configure and launch Kubernetes job.
        with ctx.obj.monitor.measure("metaflow.aws.eks.launch_job"):
            kubernetes.launch_job(
                user=util.get_username(),
                code_package_sha=code_package_sha,
                code_package_url=code_package_url,
                code_package_ds=ctx.obj.flow_datastore.TYPE,
                step_cli=step_cli,
                docker_image=image,
                service_account=service_account,
                secrets=secrets,
                node_selector=node_selector,
                namespace=k8s_namespace,
                cpu=cpu,
                gpu=gpu,
                disk=disk,
                memory=memory,
                run_time_limit=run_time_limit,
                env=env,
                tolerations=tolerations,
            )
    except Exception as e:
        traceback.print_exc()
        _sync_metadata()
        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    try:
        kubernetes.wait(stdout_location, stderr_location, echo=echo)
    except KubernetesKilledException:
        # don't retry killed tasks
        traceback.print_exc()
        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    finally:
        _sync_metadata()
