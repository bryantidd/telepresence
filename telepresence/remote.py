import json
import sys
from subprocess import STDOUT, CalledProcessError
from time import time, sleep
from typing import Optional, Dict, Tuple, Callable

from tempfile import mkdtemp

from telepresence import __version__
from telepresence.runner import Runner
from telepresence.ssh import SSH


class RemoteInfo(object):
    """
    Information about the remote setup.

    :ivar namespace str: The Kubernetes namespace.
    :ivar context str: The Kubernetes context.
    :ivar deployment_name str: The name of the Deployment object.
    :ivar pod_name str: The name of the pod created by the Deployment.
    :ivar deployment_config dict: The decoded k8s object (i.e. JSON/YAML).
    :ivar container_config dict: The container within the Deployment JSON.
    :ivar container_name str: The name of the container.
    """

    def __init__(
        self,
        runner: Runner,
        context: str,
        namespace: str,
        deployment_name: str,
        pod_name: str,
        deployment_config: dict,
    ) -> None:
        self.context = context
        self.namespace = namespace
        self.deployment_name = deployment_name
        self.pod_name = pod_name
        self.deployment_config = deployment_config
        cs = deployment_config["spec"]["template"]["spec"]["containers"]
        containers = [c for c in cs if "telepresence-k8s" in c["image"]]
        if not containers:
            raise RuntimeError(
                "Could not find container with image "
                "'datawire/telepresence-k8s' in pod {}.".format(pod_name)
            )
        self.container_config = containers[0]  # type: Dict
        self.container_name = self.container_config["name"]  # type: str

    def remote_telepresence_version(self) -> str:
        """Return the version used by the remote Telepresence container."""
        return self.container_config["image"].split(":")[-1]


def get_deployment_json(
    runner: Runner,
    deployment_name: str,
    context: str,
    namespace: str,
    deployment_type: str,
    run_id: Optional[str] = None,
) -> Dict:
    """Get the decoded JSON for a deployment.

    If this is a Deployment we created, the run_id is also passed in - this is
    the uuid we set for the telepresence label. Otherwise run_id is None and
    the Deployment name must be used to locate the Deployment.
    """
    assert context is not None
    assert namespace is not None
    try:
        get_deployment = [
            "get",
            deployment_type,
            "-o",
            "json",
            "--export",
        ]
        if run_id is None:
            return json.loads(
                runner.get_kubectl(
                    context,
                    namespace,
                    get_deployment + [deployment_name],
                    stderr=STDOUT
                )
            )
        else:
            # When using a selector we get a list of objects, not just one:
            return json.loads(
                runner.get_kubectl(
                    context,
                    namespace,
                    get_deployment + ["--selector=telepresence=" + run_id],
                    stderr=STDOUT
                )
            )["items"][0]
    except CalledProcessError as e:
        raise SystemExit(
            "Failed to find Deployment '{}': {}".format(
                deployment_name, str(e.stdout, "utf-8")
            )
        )


def wait_for_pod(runner: Runner, remote_info: RemoteInfo) -> None:
    """Wait for the pod to start running."""
    start = time()
    while time() - start < 120:
        try:
            pod = json.loads(
                runner.get_kubectl(
                    remote_info.context, remote_info.namespace,
                    ["get", "pod", remote_info.pod_name, "-o", "json"]
                )
            )
        except CalledProcessError:
            sleep(0.25)
            continue
        if pod["status"]["phase"] == "Running":
            for container in pod["status"]["containerStatuses"]:
                if container["name"] == remote_info.container_name and (
                    container["ready"]
                ):
                    return
        sleep(0.25)
    raise RuntimeError(
        "Pod isn't starting or can't be found: {}".format(pod["status"])
    )


def get_remote_info(
    runner: Runner,
    deployment_name: str,
    context: str,
    namespace: str,
    deployment_type: str,
    run_id: Optional[str] = None,
) -> RemoteInfo:
    """
    Given the deployment name, return a RemoteInfo object.

    If this is a Deployment we created, the run_id is also passed in - this is
    the uuid we set for the telepresence label. Otherwise run_id is None and
    the Deployment name must be used to locate the Deployment.
    """
    deployment = get_deployment_json(
        runner,
        deployment_name,
        context,
        namespace,
        deployment_type,
        run_id=run_id
    )
    expected_metadata = deployment["spec"]["template"]["metadata"]
    runner.write("Expected metadata for pods: {}\n".format(expected_metadata))

    start = time()
    while time() - start < 120:
        pods = json.loads(
            runner.get_kubectl(
                context, namespace, ["get", "pod", "-o", "json", "--export"]
            )
        )["items"]
        for pod in pods:
            name = pod["metadata"]["name"]
            phase = pod["status"]["phase"]
            runner.write(
                "Checking {} (phase {})...\n".format(
                    pod["metadata"].get("labels"), phase
                )
            )
            if not set(expected_metadata.get("labels", {}).items()).issubset(
                set(pod["metadata"].get("labels", {}).items())
            ):
                runner.write("Labels don't match.\n")
                continue
            # Metadata for Deployment will hopefully have a namespace. If not,
            # fall back to one we were given. If we weren't given one, best we
            # can do is choose "default".
            if (
                name.startswith(deployment_name + "-")
                and pod["metadata"]["namespace"] == deployment["metadata"].get(
                    "namespace", namespace
                ) and phase in ("Pending", "Running")
            ):
                runner.write("Looks like we've found our pod!\n")
                remote_info = RemoteInfo(
                    runner,
                    context,
                    namespace,
                    deployment_name,
                    name,
                    deployment,
                )
                # Ensure remote container is running same version as we are:
                if remote_info.remote_telepresence_version() != __version__:
                    raise SystemExit((
                        "The remote datawire/telepresence-k8s container is " +
                        "running version {}, but this tool is version {}. " +
                        "Please make sure both are running the same version."
                    ).format(
                        remote_info.remote_telepresence_version(), __version__
                    ))
                # Wait for pod to be running:
                wait_for_pod(runner, remote_info)
                return remote_info

        # Didn't find pod...
        sleep(1)

    raise RuntimeError(
        "Telepresence pod not found for Deployment '{}'.".
        format(deployment_name)
    )


def mount_remote_volumes(
    runner: Runner, remote_info: RemoteInfo, ssh: SSH, allow_all_users: bool
) -> Tuple[str, Callable]:
    """
    sshfs is used to mount the remote system locally.

    Allowing all users may require root, so we use sudo in that case.

    Returns (path to mounted directory, callable that will unmount it).
    """
    # Docker for Mac only shares some folders; the default TMPDIR on OS X is
    # not one of them, so make sure we use /tmp:
    mount_dir = mkdtemp(dir="/tmp")
    sudo_prefix = ["sudo"] if allow_all_users else []
    middle = ["-o", "allow_other"] if allow_all_users else []
    try:
        runner.check_call(
            sudo_prefix + [
                "sshfs",
                "-p",
                str(ssh.port),
                # Don't load config file so it doesn't break us:
                "-F",
                "/dev/null",
                # Don't validate host key:
                "-o",
                "StrictHostKeyChecking=no",
                # Don't store host key:
                "-o",
                "UserKnownHostsFile=/dev/null",
            ] + middle + ["telepresence@localhost:/", mount_dir]
        )
        mounted = True
    except CalledProcessError:
        print(
            "Mounting remote volumes failed, they will be unavailable"
            " in this session. If you are running"
            " on Windows Subystem for Linux then see"
            " https://github.com/datawire/telepresence/issues/115,"
            " otherwise please report a bug, attaching telepresence.log to"
            " the bug report:"
            " https://github.com/datawire/telepresence/issues/new",
            file=sys.stderr
        )
        mounted = False

    def no_cleanup():
        pass

    def cleanup():
        if sys.platform.startswith("linux"):
            runner.check_call(
                sudo_prefix + ["fusermount", "-z", "-u", mount_dir]
            )
        else:
            runner.get_output(sudo_prefix + ["umount", "-f", mount_dir])

    return mount_dir, cleanup if mounted else no_cleanup
