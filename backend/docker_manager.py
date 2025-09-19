import docker
import asyncio
import logging
from typing import Optional, Dict, Any
import json
import uuid
from pprint import pprint

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()
        self.containers: Dict[str, docker.models.containers.Container] = {}

    async def create_container(self, session_id: str, image: str = "ctf-agent:latest") -> str:
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
                    "DEBIAN_FRONTEND": "noninteractive"
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
            
            # Test basic functionality
            test_result = await self._test_container_functionality(session_id)
            if not test_result["success"]:
                logger.warning(f"Container {session_id} basic functionality test failed: {test_result['error']}")
            
            logger.info(f"Created container {container.id} for session {session_id}")
            return container.id

        except Exception as e:
            logger.error(f"Failed to create container for session {session_id}: {e}")
            raise

    async def execute_command(self, session_id: str, command: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute a command in the container."""
        if session_id not in self.containers:
            raise ValueError(f"No container found for session {session_id}")

        container = self.containers[session_id]
        
        try:
            # Check container status before execution
            container.reload()
            if container.status != 'running':
                logger.error(f"Container {session_id} is not running: {container.status}")
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Container is not running (status: {container.status})",
                    "success": False,
                    "container_status": container.status
                }
            
            logger.info(f"Executing command in {session_id}: {command}")
            
            # Execute command - use shell for complex commands with pipes, &&, etc.
            needs_shell = any(op in command for op in ['&&', '||', '|', '>', '<', ';', '$(', '`'])
            
            if needs_shell:
                # Wrap command in shell for complex operations
                exec_command = ['/bin/bash', '-c', command]
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
                workdir="/workspace"
            )

            pprint(exec_result)
            
            stdout = exec_result.output[0].decode('utf-8') if exec_result.output[0] else ""
            stderr = exec_result.output[1].decode('utf-8') if exec_result.output[1] else ""
            
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
                "container_status": container.status
            }
            
        except Exception as e:
            logger.error(f"Command execution failed in session {session_id}: {e}")
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Execution error: {str(e)}",
                "success": False,
                "container_status": "error"
            }

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
            tar = tarfile.TarFile(fileobj=tar_stream, mode='w')
            
            file_data = content.encode('utf-8')
            tarinfo = tarfile.TarInfo(name=path.split('/')[-1])
            tarinfo.size = len(file_data)
            
            tar.addfile(tarinfo, io.BytesIO(file_data))
            tar.close()
            tar_stream.seek(0)
            
            # Copy to container
            container.put_archive(path=path.rsplit('/', 1)[0] or '/', data=tar_stream.read())
            return True
            
        except Exception as e:
            logger.error(f"Failed to copy file to container {session_id}: {e}")
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
                logger.error(f"Failed to cleanup container for session {session_id}: {e}")
            finally:
                del self.containers[session_id]

    def get_container_logs(self, session_id: str, tail: int = 100) -> str:
        """Get container logs."""
        if session_id not in self.containers:
            return ""
        
        container = self.containers[session_id]
        try:
            logs = container.logs(tail=tail, timestamps=True)
            return logs.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to get logs for session {session_id}: {e}")
            return f"Error getting logs: {e}"

    async def ensure_image_exists(self, image: str = "ctf-agent:latest"):
        """Ensure the CTF agent Docker image exists."""
        try:
            self.client.images.get(image)
            logger.info(f"Image {image} already exists")
        except docker.errors.ImageNotFound:
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
                        "error": f"{description} failed: exit code {result['exit_code']}, stderr: {result['stderr']}"
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"{description} test exception: {str(e)}"
                }
        
        return {"success": True, "error": None}
