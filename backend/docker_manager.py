import docker
from docker.errors import ImageNotFound
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple, Callable, Coroutine
from docker.models.containers import Container
import json
import uuid
import time
from pprint import pprint
import shlex

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self):
        self.client = docker.from_env(timeout=15)
        self.containers: Dict[str, Container] = {}
        # Cache of (uid, gid) for ctfuser per session/container
        self._ctfuser_ids: Dict[str, Tuple[int, int]] = {}

    async def create_container(
        self, session_id: str, image: str = "ctf-agent:latest"
    ) -> str:
        """Create a new Docker container for CTF challenge solving."""
        try:
            container_name = f"ctf-agent-{session_id}"

            # Container configuration
            container_config = {
                "image": image,
                "name": container_name,
                "detach": True,
                "tty": True,
                "stdin_open": True,
                "working_dir": "/workspace",
                "environment": {
                    "PYTHONUNBUFFERED": "1",
                    "DEBIAN_FRONTEND": "noninteractive",
                },
                "volumes": {
                    # Mount a volume for persistent workspace
                    f"ctf-workspace-{session_id}": {"bind": "/workspace", "mode": "rw"}
                },
                "network_mode": "bridge",
                # Security settings
                "cap_drop": ["ALL"],
                "cap_add": ["NET_RAW", "NET_ADMIN"],  # For network tools
                "security_opt": ["no-new-privileges:true"],
                "read_only": False,  # Allow writes to workspace
                "tmpfs": {"/tmp": "noexec,nosuid,size=100m"},
            }

            creation = asyncio.create_task(
                asyncio.to_thread(self.client.containers.run, **container_config)
            )
            try:
                container = await asyncio.shield(creation)
            except asyncio.CancelledError:
                # Docker creation cannot be cancelled once its worker has started.
                container = await creation
                self.containers[session_id] = container
                await self.cleanup_container(session_id)
                raise
            self.containers[session_id] = container

            # Wait a moment for container to fully start
            await asyncio.sleep(2)

            # Ensure /workspace and handouts dir are owned by ctfuser even with a fresh volume
            try:
                await asyncio.to_thread(
                    container.exec_run,
                    [
                        "/bin/bash",
                        "-c",
                        "mkdir -p /workspace/handouts && chown -R ctfuser:ctfuser /workspace && chmod 775 /workspace /workspace/handouts",
                    ],
                    stdout=False,
                    stderr=False,
                    user="root",
                )
            except Exception:
                # Best effort; not fatal
                pass

            # Test basic functionality
            test_result = await self._test_container_functionality(session_id)
            if not test_result["success"]:
                logger.warning(
                    f"Container {session_id} basic functionality test failed: {test_result['error']}"
                )

            logger.info(f"Created container {container.id} for session {session_id}")
            return container.id

        except Exception as e:
            logger.error(f"Failed to create container for session {session_id}: {e}")
            raise

    async def execute_command(
        self, session_id: str, command: str, timeout: int = 30
    ) -> Dict[str, Any]:
        """Execute a command in the container."""
        return await self.execute_command_stream(
            session_id=session_id,
            command=command,
            on_chunk=None,
            timeout=float(timeout) if timeout is not None else None,
        )

    async def execute_command_stream(
        self, session_id, command, on_chunk=None, timeout=None, stop_event=None
    ):
        if session_id not in self.containers:
            raise ValueError(f"No container found for {session_id}")
        container = self.containers[session_id]
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        stdout, stderr = [], []
        timed_out = False
        stopped = False

        def produce():
            try:
                ident = self.client.api.exec_create(
                    container.id,
                    ["/bin/bash", "-c", command],
                    stdout=True,
                    stderr=True,
                    tty=False,
                    user="ctfuser",
                )["Id"]
                for out, err in self.client.api.exec_start(
                    ident, stream=True, demux=True
                ):
                    for channel, chunk in (("stdout", out), ("stderr", err)):
                        if chunk:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                (channel, chunk.decode("utf-8", errors="replace")),
                            )
                code = self.client.api.exec_inspect(ident).get("ExitCode")
                return code if code is not None else -1
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async def consume():
            while True:
                item = await queue.get()
                if item is None:
                    return
                channel, text = item
                (stdout if channel == "stdout" else stderr).append(text)
                if on_chunk:
                    await on_chunk(channel, text)

        async def terminate():
            try:
                await asyncio.to_thread(container.kill)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError:
                logger.debug("Container already stopped", exc_info=True)

        runner = asyncio.create_task(asyncio.to_thread(produce))
        consumer = asyncio.create_task(consume())
        waiter = asyncio.create_task(stop_event.wait()) if stop_event else None
        try:
            tasks = {runner} | ({waiter} if waiter else set())
            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            timed_out = not done
            stopped = bool(stop_event and stop_event.is_set())
            if timed_out or stopped:
                await terminate()
            code = await runner
            await consumer  # Flush every callback before finalizing the action.
            if stop_event and stop_event.is_set() and not stopped:
                stopped = True
                await terminate()
            return dict(
                exit_code=code,
                stdout="".join(stdout),
                stderr="".join(stderr),
                success=code == 0 and not timed_out,
                timed_out=timed_out,
                container_status="stopped" if stopped or timed_out else "running",
            )
        except BaseException:
            await terminate()
            await asyncio.gather(runner, consumer, return_exceptions=True)
            raise
        finally:
            if waiter:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    def _get_ctfuser_ids(self, session_id: str) -> Tuple[int, int]:
        """Lookup and cache the uid,gid for user 'ctfuser' inside the container."""
        if session_id in self._ctfuser_ids:
            return self._ctfuser_ids[session_id]

        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        container = self.containers[session_id]
        try:
            uid_res = container.exec_run(
                ["/bin/bash", "-c", "id -u ctfuser"],
                stdout=True,
                stderr=True,
                user="root",
                demux=True,
            )
            gid_res = container.exec_run(
                ["/bin/bash", "-c", "id -g ctfuser"],
                stdout=True,
                stderr=True,
                user="root",
                demux=True,
            )
            uid_out = (
                uid_res.output[0].decode("utf-8").strip() if uid_res.output else "1000"
            )
            gid_out = (
                gid_res.output[0].decode("utf-8").strip() if gid_res.output else "1000"
            )
            uid = int(uid_out or 1000)
            gid = int(gid_out or 1000)
        except Exception:
            # Fallback to common default UID/GID
            uid, gid = 1000, 1000

        self._ctfuser_ids[session_id] = (uid, gid)
        return uid, gid

    def _chown_path_ctfuser(self, session_id: str, path: str) -> None:
        """Best-effort chown of a single path to ctfuser:ctfuser (run as root)."""
        try:
            if session_id not in self.containers:
                return
            container = self.containers[session_id]
            container.exec_run(
                ["/bin/bash", "-c", f"chown -h ctfuser:ctfuser '{path}'"],
                stdout=False,
                stderr=False,
                user="root",
            )
        except Exception:
            # Best effort only
            pass

    async def copy_to_container(self, session_id: str, content: str, path: str) -> bool:
        """Copy content to a file in the container."""
        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        container = self.containers[session_id]

        try:
            import tarfile
            import io

            # Create tar archive with the content
            tar_stream = io.BytesIO()
            tar = tarfile.TarFile(fileobj=tar_stream, mode="w")

            file_data = content.encode("utf-8")
            tarinfo = tarfile.TarInfo(name=path.split("/")[-1])
            tarinfo.size = len(file_data)
            # Ensure the file is owned by ctfuser inside the container
            uid, gid = self._get_ctfuser_ids(session_id)
            tarinfo.uid = uid
            tarinfo.gid = gid
            tarinfo.uname = "ctfuser"
            tarinfo.gname = "ctfuser"

            tar.addfile(tarinfo, io.BytesIO(file_data))
            tar.close()
            tar_stream.seek(0)

            # Copy to container
            container.put_archive(
                path=path.rsplit("/", 1)[0] or "/", data=tar_stream.read()
            )
            # Fallback chown in case tar ownership was not honored
            self._chown_path_ctfuser(session_id, path)
            return True

        except Exception as e:
            logger.error(f"Failed to copy file to container {session_id}: {e}")
            return False

    async def copy_local_file_to_container(
        self, session_id: str, host_path: str, container_dest_path: str
    ) -> bool:
        """Copy a local host file into the container exactly (binary-safe)."""
        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        import os
        import tarfile
        import io

        container = self.containers[session_id]
        try:
            # Ensure destination directory exists in container
            dest_dir = container_dest_path.rsplit("/", 1)[0] or "/"
            try:
                container.exec_run(
                    [
                        "/bin/bash",
                        "-c",
                        f"mkdir -p '{dest_dir}' && chown ctfuser:ctfuser '{dest_dir}'",
                    ],
                    stdout=False,
                    stderr=False,
                    user="root",
                )
            except Exception:
                pass

            # Create tar archive containing the file with the correct filename
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                with open(host_path, "rb") as f:
                    data = f.read()
                arcname = container_dest_path.split("/")[-1]
                tarinfo = tarfile.TarInfo(name=arcname)
                tarinfo.size = len(data)
                # Ensure the file is owned by ctfuser inside the container
                uid, gid = self._get_ctfuser_ids(session_id)
                tarinfo.uid = uid
                tarinfo.gid = gid
                tarinfo.uname = "ctfuser"
                tarinfo.gname = "ctfuser"
                tar.addfile(tarinfo, io.BytesIO(data))
            tar_stream.seek(0)

            # Put archive into destination directory
            container.put_archive(path=dest_dir, data=tar_stream.read())
            # Fallback chown in case tar ownership was not honored
            self._chown_path_ctfuser(session_id, container_dest_path)
            return True
        except Exception as e:
            logger.error(
                f"Failed to copy local file '{host_path}' to container {session_id} at '{container_dest_path}': {e}"
            )
            return False

    async def cleanup_container(self, session_id: str):
        """Clean up container and associated resources."""
        if session_id in self.containers:
            container = self.containers[session_id]
            try:
                await asyncio.to_thread(container.stop, timeout=2)
                await asyncio.to_thread(container.remove, force=True)
                logger.info(f"Cleaned up container for session {session_id}")
            except Exception as e:
                logger.error(
                    f"Failed to cleanup container for session {session_id}: {e}"
                )
            finally:
                self.containers.pop(session_id, None)

        # Remove the named volume created for this session's workspace
        try:
            vol_name = f"ctf-workspace-{session_id}"
            volume = self.client.volumes.get(vol_name)
            try:
                await asyncio.to_thread(volume.remove, force=True)
                logger.info(f"Removed volume {vol_name} for session {session_id}")
            except Exception as ve:
                logger.warning(
                    f"Failed to remove volume {vol_name} for session {session_id}: {ve}"
                )
        except Exception:
            # Volume may not exist; ignore
            pass

    async def kill_container(self, session_id: str, remove: bool = True):
        """Immediately kill a running container (SIGKILL) and optionally remove it.

        This is intended for hard-stop scenarios (e.g., user pressed Stop) where
        we cannot wait for graceful shutdown or long-running execs to finish.
        """
        if session_id not in self.containers:
            return

        container = self.containers[session_id]
        try:
            try:
                await asyncio.to_thread(container.kill)  # SIGKILL for immediate stop
                logger.info(f"Killed container for session {session_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to kill container for session {session_id}: {e}"
                )

            if remove:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                    logger.info(
                        f"Removed container after kill for session {session_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to remove container after kill for session {session_id}: {e}"
                    )
        finally:
            # Ensure we drop reference regardless of success
            try:
                self.containers.pop(session_id, None)
            except Exception:
                pass

            # Attempt to remove the session's named volume as well
            try:
                vol_name = f"ctf-workspace-{session_id}"
                volume = self.client.volumes.get(vol_name)
                try:
                    await asyncio.to_thread(volume.remove, force=True)
                    logger.info(
                        f"Removed volume {vol_name} after kill for session {session_id}"
                    )
                except Exception as ve:
                    logger.warning(
                        f"Failed to remove volume {vol_name} after kill for session {session_id}: {ve}"
                    )
            except Exception:
                # Volume may not exist; ignore
                pass

    def get_container_logs(self, session_id: str, tail: int = 100) -> str:
        """Get container logs."""
        if session_id not in self.containers:
            return ""

        container = self.containers[session_id]
        try:
            logs = container.logs(tail=tail, timestamps=True)
            return logs.decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to get logs for session {session_id}: {e}")
            return f"Error getting logs: {e}"

    async def ensure_image_exists(self, image: str = "ctf-agent:latest"):
        """Ensure the CTF agent Docker image exists."""
        try:
            self.client.images.get(image)
            logger.info(f"Image {image} already exists")
        except ImageNotFound:
            logger.info(f"Image {image} not found, building...")
            # Image will be built by Docker configuration
            raise ValueError(f"Image {image} not found. Please build it first.")

    async def _test_container_functionality(self, session_id: str) -> Dict[str, Any]:
        """Test basic container functionality."""
        tests = [
            ("echo 'test'", "Basic shell functionality"),
            ("curl --version", "curl availability"),
            ("python3 --version", "Python availability"),
            ("ls /workspace", "Workspace access"),
        ]

        for command, description in tests:
            try:
                result = await self.execute_command(session_id, command, timeout=10)
                if not result["success"]:
                    return {
                        "success": False,
                        "error": f"{description} failed: exit code {result['exit_code']}, stderr: {result['stderr']}",
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"{description} test exception: {str(e)}",
                }

        return {"success": True, "error": None}
