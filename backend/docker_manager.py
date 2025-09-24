import docker
from docker.errors import ImageNotFound
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple
from docker.models.containers import Container
import json
import uuid
from pprint import pprint

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()
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

            container = self.client.containers.run(**container_config)
            self.containers[session_id] = container

            # Wait a moment for container to fully start
            await asyncio.sleep(2)

            # Ensure /workspace and handouts dir are owned by ctfuser even with a fresh volume
            try:
                container.exec_run(
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
        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        container = self.containers[session_id]

        try:
            # Check container status before execution
            container.reload()
            if container.status != "running":
                logger.error(
                    f"Container {session_id} is not running: {container.status}"
                )
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Container is not running (status: {container.status})",
                    "success": False,
                    "container_status": container.status,
                }

            logger.info(f"Executing command in {session_id}: {command}")

            # Execute command - use shell for complex commands with pipes, &&, etc.
            needs_shell = any(
                op in command for op in ["&&", "||", "|", ">", "<", ";", "$(", "`"]
            )

            if needs_shell:
                # Wrap command in shell for complex operations
                exec_command = ["/bin/bash", "-c", command]
                logger.debug(f"Using shell execution: {exec_command}")
            else:
                # Simple command can be executed directly
                exec_command = command
                logger.debug(f"Using direct execution: {exec_command}")

            exec_result = container.exec_run(
                exec_command,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                detach=False,
                demux=True,
                workdir="/workspace",
            )

            stdout = (
                exec_result.output[0].decode("utf-8") if exec_result.output[0] else ""
            )
            stderr = (
                exec_result.output[1].decode("utf-8") if exec_result.output[1] else ""
            )

            logger.info(f"Command completed with exit code {exec_result.exit_code}")
            # if stdout:
            logger.debug(f"STDOUT: {stdout[:200]}...")
            # if stderr:
            logger.debug(f"STDERR: {stderr[:200]}...")

            return {
                "exit_code": exec_result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "success": exec_result.exit_code == 0,
                "container_status": container.status,
            }

        except Exception as e:
            logger.error(f"Command execution failed in session {session_id}: {e}")
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Execution error: {str(e)}",
                "success": False,
                "container_status": "error",
            }

    async def execute_command_stream(
        self,
        session_id: str,
        command: str,
        on_chunk,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Execute a command and stream output via an async callback.

        on_chunk(channel: str, text: str) -> Awaitable[None]
        channel is one of "stdout" | "stderr".
        """
        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        container = self.containers[session_id]

        try:
            # Check container status before execution
            container.reload()
            if container.status != "running":
                await on_chunk("stderr", f"Container is not running (status: {container.status})\n")
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Container is not running (status: {container.status})",
                    "success": False,
                    "container_status": container.status,
                }

            logger.info(f"Executing (stream) in {session_id}: {command}")

            # Execute command - use shell for complex commands with pipes, &&, etc.
            needs_shell = any(op in command for op in ["&&", "||", "|", ">", "<", ";", "$(", "`"])
            if needs_shell:
                exec_command = ["/bin/bash", "-lc", command]
            else:
                exec_command = command

            stdout_acc: list[str] = []
            stderr_acc: list[str] = []

            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()

            def _run_and_stream() -> int:
                try:
                    # Use low-level API for streaming
                    exec_id = self.client.api.exec_create(
                        container.id,
                        exec_command,
                        stdout=True,
                        stderr=True,
                        stdin=False,
                        tty=False,
                    )["Id"]

                    # Start streaming
                    stream = self.client.api.exec_start(exec_id, stream=True, demux=True)
                    for out, err in stream:
                        if out:
                            text = out.decode("utf-8", errors="replace")
                            stdout_acc.append(text)
                            _asyncio.run_coroutine_threadsafe(on_chunk("stdout", text), loop)
                        if err:
                            text = err.decode("utf-8", errors="replace")
                            stderr_acc.append(text)
                            _asyncio.run_coroutine_threadsafe(on_chunk("stderr", text), loop)

                    info = self.client.api.exec_inspect(exec_id)
                    code = info.get("ExitCode", -1)
                    return int(code if code is not None else -1)
                except Exception as e:
                    _asyncio.run_coroutine_threadsafe(on_chunk("stderr", f"Execution error: {e}\n"), loop)
                    return -1

            exit_code: int = await _asyncio.to_thread(_run_and_stream)

            return {
                "exit_code": exit_code,
                "stdout": "".join(stdout_acc),
                "stderr": "".join(stderr_acc),
                "success": exit_code == 0,
                "container_status": container.status,
            }

        except Exception as e:
            logger.error(f"Streaming execution failed in session {session_id}: {e}")
            try:
                await on_chunk("stderr", f"Execution error: {str(e)}\n")
            except Exception:
                pass
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Execution error: {str(e)}",
                "success": False,
                "container_status": "error",
            }

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
            uid_out = uid_res.output[0].decode("utf-8").strip() if uid_res.output else "1000"
            gid_out = gid_res.output[0].decode("utf-8").strip() if gid_res.output else "1000"
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
                container.stop(timeout=10)
                container.remove()
                logger.info(f"Cleaned up container for session {session_id}")
            except Exception as e:
                logger.error(
                    f"Failed to cleanup container for session {session_id}: {e}"
                )
            finally:
                del self.containers[session_id]

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
                container.kill()  # SIGKILL for immediate stop
                logger.info(f"Killed container for session {session_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to kill container for session {session_id}: {e}"
                )

            if remove:
                try:
                    container.remove(force=True)
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
                del self.containers[session_id]
            except Exception:
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
