import asyncio
import os
import re
import time
from abc import ABC
from dataclasses import asdict, dataclass, field
from logging import INFO, basicConfig, getLogger
from typing import Any, Dict, List, Optional, Union

import docker
import docker.errors
import docker.types
from huggingface_hub import AsyncInferenceClient

from .utils import get_free_port

basicConfig(level=INFO)

DOCKER = docker.from_env()
LOGGER = getLogger("docker-inference-server")


@dataclass
class DockerInferenceServerConfig:
    # Image to use for the container
    image: str
    # Shared memory size for the container
    shm_size: str = "1g"
    # List of custom devices to forward to the container e.g. ["/dev/kfd", "/dev/dri"] for ROCm
    devices: Optional[List[str]] = None
    # NVIDIA-docker GPU device options e.g. "all" (all) or "0,1,2,3" (ids) or 4 (count)
    gpus: Optional[Union[str, int]] = None

    ports: Dict[str, Any] = field(
        default_factory=lambda: {"80/tcp": ("127.0.0.1", 0)},
        metadata={"help": "Dictionary of ports to expose from the container."},
    )
    volumes: Dict[str, Any] = field(
        default_factory=lambda: {os.path.expanduser("~/.cache/huggingface/hub"): {"bind": "/data", "mode": "rw"}},
        metadata={"help": "Dictionary of volumes to mount inside the container."},
    )
    environment: Dict[str, str] = field(
        default_factory=lambda: {"HUGGINGFACE_HUB_TOKEN": os.environ.get("HUGGINGFACE_HUB_TOKEN", "")},
        metadata={"help": "Dictionary of environment variables to forward to the container."},
    )

    timeout: int = 60

    def __post_init__(self) -> None:
        if self.ports["80/tcp"][1] == 0:
            LOGGER.info("\t+ Getting a free port for the server")
            self.ports["80/tcp"] = (self.ports["80/tcp"][0], get_free_port())


class DockerInferenceServer(ABC):
    NAME: str = "Docker-Inference-Server"
    SUCCESS_SENTINEL: str = "Success"
    FAILURE_SENTINEL: str = "Failure"

    def __init__(self, config: DockerInferenceServerConfig) -> None:
        self.config = config

        try:
            LOGGER.info(f"\t+ Checking if {self.NAME} image is available locally")
            DOCKER.images.get(self.config.image)
            LOGGER.info(f"\t+ {self.NAME} image found locally")
        except docker.errors.ImageNotFound:
            LOGGER.info(f"\t+ {self.NAME} image not found locally, pulling from Docker Hub")
            DOCKER.images.pull(self.config.image)

        if self.config.gpus is not None and isinstance(self.config.gpus, str) and self.config.gpus == "all":
            LOGGER.info("\t+ Using all GPU(s)")
            self.device_requests = [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
        elif self.config.gpus is not None and isinstance(self.config.gpus, int):
            LOGGER.info(f"\t+ Using {self.config.gpus} GPU(s)")
            self.device_requests = [docker.types.DeviceRequest(count=self.config.gpus, capabilities=[["gpu"]])]
        elif (
            self.config.gpus is not None
            and isinstance(self.config.gpus, str)
            and re.match(r"^\d+(,\d+)*$", self.config.gpus)
        ):
            LOGGER.info(f"\t+ Using GPU(s) {self.config.gpus}")
            self.device_requests = [docker.types.DeviceRequest(device_ids=[self.config.gpus], capabilities=[["gpu"]])]
        else:
            LOGGER.info("\t+ Not using any GPU(s)")
            self.device_requests = None

        LOGGER.info(f"\t+ Building {self.NAME} command")
        self.command = []
        for k, v in asdict(self.config).items():
            if k in DockerInferenceServerConfig.__annotations__:
                continue
            elif v is not None:
                if isinstance(v, bool):
                    self.command.append(f"--{k.replace('_', '-')}")
                else:
                    self.command.append(f"--{k.replace('_', '-')}={v}")

        address, port = self.config.ports["80/tcp"]
        self.url = f"http://{address}:{port}"

        LOGGER.info(f"\t+ Running {self.NAME} container")
        self.container = DOCKER.containers.run(
            image=self.config.image,
            ports=self.config.ports,
            volumes=self.config.volumes,
            devices=self.config.devices,
            shm_size=self.config.shm_size,
            environment=self.config.environment,
            device_requests=self.device_requests,
            command=self.command,
            auto_remove=True,
            detach=True,
        )

        LOGGER.info(f"\t+ Streaming {self.NAME} server logs")
        for line in self.container.logs(stream=True):
            log = line.decode("utf-8").strip()
            if self.SUCCESS_SENTINEL.lower() in log.lower():
                LOGGER.info(f"\t {log}")
                break
            elif self.FAILURE_SENTINEL.lower() in log.lower():
                LOGGER.info(f"\t {log}")
                raise Exception(f"{self.NAME} server failed to start")
            else:
                LOGGER.info(f"\t {log}")

        LOGGER.info(f"\t+ Waiting for {self.NAME} server to be ready")
        start_time = time.time()
        while time.time() - start_time < self.config.timeout:
            try:
                if not hasattr(self, "client"):
                    self.client = AsyncInferenceClient(model=self.url)

                asyncio.run(self.single_client_call(f"Hello {self.NAME}!"))
                LOGGER.info(f"\t+ Connected to {self.NAME} server successfully")
                break
            except Exception:
                LOGGER.info(f"\t+ {self.NAME} server is not ready yet, waiting 1 second")
                time.sleep(1)

    async def single_client_call(self, *args, **kwargs) -> Any:
        raise NotImplementedError

    async def batch_client_call(self, *args, **kwargs) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        if hasattr(self, "container"):
            LOGGER.info("\t+ Stoping Docker container")
            self.container.stop()
            self.container.wait()
            LOGGER.info("\t+ Docker container stopped")
            del self.container

        if hasattr(self, "client"):
            del self.client

    def __del__(self) -> None:
        self.close()
