"""Kernel Provisioner Classes"""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import asyncio
import os
import signal
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..connect import KernelConnectionInfo, LocalPortCache
from ..launcher import launch_kernel, launch_proxy
from ..localinterfaces import is_local_ip, local_ips
from .provisioner_base import KernelProvisionerBase


class LocalProvisioner(KernelProvisionerBase):  # type:ignore[misc]
    """
    :class:`LocalProvisioner` is a concrete class of ABC :py:class:`KernelProvisionerBase`
    and is the out-of-box default implementation used when no kernel provisioner is
    specified in the kernel specification (``kernel.json``).  It provides functional
    parity to existing applications by launching the kernel locally and using
    :class:`subprocess.Popen` to manage its lifecycle.

    This class is intended to be subclassed for customizing local kernel environments
    and serve as a reference implementation for other custom provisioners.
    """
    PROXY = 0
    KERNEL = 1
    processes = [None, None]
    #process = None
    _exit_future = None
    # pid = None
    # pgid = None
    pids = [None, None]
    pgids = [None, None]
    ip = None
    ports_cached = False

    @property
    def has_process(self) -> bool:
        return self.processes[self.KERNEL] is not None
        #return self.process is not None

    async def poll(self) -> Optional[int]:
        """Poll the provisioner."""
        rets = [0, 0]
        for id, process in enumerate(self.processes):
            if process:
                rets[id] = process.poll()  # type:ignore[unreachable]
        for _, ret in enumerate(rets):
            if ret != 0:
                return ret
        return 0
        # if self.process:
        #     ret = self.process.poll()  # type:ignore[unreachable]

    async def _poll(self, process) -> Optional[int]:
        """Poll the provisioner."""
        ret = 0
        if process:
            ret = process.poll()  # type:ignore[unreachable]
        return ret

    async def wait(self) -> Optional[int]:
        """Wait for the provisioner process."""
        rets = [0, 0]
        for id, process in enumerate(self.processes):
             if process:
                # Use busy loop at 100ms intervals, polling until the process is
                # not alive.  If we find the process is no longer alive, complete
                # its cleanup via the blocking wait().  Callers are responsible for
                # issuing calls to wait() using a timeout (see kill()).
                while await self._poll(process) is None:  # type:ignore[unreachable]
                    await asyncio.sleep(0.1)

                # Process is no longer alive, wait and clear
                rets[id] = process.wait()
                # Make sure all the fds get closed.
                for attr in ["stdout", "stderr", "stdin"]:
                    fid = getattr(process, attr)
                    if fid:
                        fid.close()
                self.processes[id] = None  # allow has_process to now return False
        for _, ret in enumerate(rets):
            if ret != 0:
                return ret
        return 0 
        # if self.process:
        #     # Use busy loop at 100ms intervals, polling until the process is
        #     # not alive.  If we find the process is no longer alive, complete
        #     # its cleanup via the blocking wait().  Callers are responsible for
        #     # issuing calls to wait() using a timeout (see kill()).
        #     while await self.poll() is None:  # type:ignore[unreachable]
        #         await asyncio.sleep(0.1)

        #     # Process is no longer alive, wait and clear
        #     ret = self.process.wait()
        #     # Make sure all the fds get closed.
        #     for attr in ["stdout", "stderr", "stdin"]:
        #         fid = getattr(self.process, attr)
        #         if fid:
        #             fid.close()
        #     self.process = None  # allow has_process to now return False
        # return ret

    async def _send_signal(self, process, pgid: int, signum: int) -> None:
        if process:
            if signum == signal.SIGINT and sys.platform == "win32":  # type:ignore[unreachable]
                from ..win_interrupt import send_interrupt

                send_interrupt(self.process.win32_interrupt_event)
                return

            # Prefer process-group over process
            if pgid and hasattr(os, "killpg"):
                try:
                    os.killpg(pgid, signum)
                    return
                except OSError:
                    pass  # We'll retry sending the signal to only the process below

            # If we're here, send the signal to the process and let caller handle exceptions
            process.send_signal(signum)

    async def send_signal(self, signum: int) -> None:
        """Sends a signal to the process group of the kernel (this
        usually includes the kernel and any subprocesses spawned by
        the kernel).

        Note that since only SIGTERM is supported on Windows, we will
        check if the desired signal is for interrupt and apply the
        applicable code on Windows in that case.
        """
        for id, process in enumerate(self.processes):
            if process:
                if signum == signal.SIGINT and sys.platform == "win32":  # type:ignore[unreachable]
                    from ..win_interrupt import send_interrupt

                    send_interrupt(self.process.win32_interrupt_event)
                    return

                # Prefer process-group over process
                if self.pgids[id] and hasattr(os, "killpg"):
                    try:
                        os.killpg(self.pgids[id], signum)
                        return
                    except OSError:
                        pass  # We'll retry sending the signal to only the process below

                # If we're here, send the signal to the process and let caller handle exceptions
                process.send_signal(signum)
        # if self.process:
        #     if signum == signal.SIGINT and sys.platform == "win32":  # type:ignore[unreachable]
        #         from ..win_interrupt import send_interrupt

        #         send_interrupt(self.process.win32_interrupt_event)
        #         return

        #     # Prefer process-group over process
        #     if self.pgid and hasattr(os, "killpg"):
        #         try:
        #             os.killpg(self.pgid, signum)
        #             return
        #         except OSError:
        #             pass  # We'll retry sending the signal to only the process below

        #     # If we're here, send the signal to the process and let caller handle exceptions
        #     self.process.send_signal(signum)
        #     return

    async def kill(self, restart: bool = False) -> None:
        """Kill the provisioner and optionally restart."""
        for id, process in enumerate(self.processes):
            if process:
                if hasattr(signal, "SIGKILL"):  # type:ignore[unreachable]
                    # If available, give preference to signalling the process-group over `kill()`.
                    try:
                        await self._send_signal(process, self.pgids[id], signal.SIGKILL)
                        return
                    except OSError:
                        pass
                try:
                    process.kill()
                except OSError as e:
                    # WARNING: This code is not correct, because we might raise
                    # and not complete the killing of the second process. We should
                    # catch all exceptions and iterate over the results like we do for rets
                    # in other functions.
                    LocalProvisioner._tolerate_no_process(e)
        # if self.process:
        #     if hasattr(signal, "SIGKILL"):  # type:ignore[unreachable]
        #         # If available, give preference to signalling the process-group over `kill()`.
        #         try:
        #             await self.send_signal(signal.SIGKILL)
        #             return
        #         except OSError:
        #             pass
        #     try:
        #         self.process.kill()
        #     except OSError as e:
        #         LocalProvisioner._tolerate_no_process(e)

    async def terminate(self, restart: bool = False) -> None:
        """Terminate the provisioner and optionally restart."""
        for id, process in enumerate(self.processes):
            if process:
                if hasattr(signal, "SIGTERM"):  # type:ignore[unreachable]
                    # If available, give preference to signalling the process group over `terminate()`.
                    try:
                        await self._send_signal(process, self.pgids[id], signal.SIGTERM)
                        return
                    except OSError:
                        pass
                try:
                    process.terminate()
                except OSError as e:
                    # WARNING: This code is not correct, because we might raise
                    # and not complete the killing of the second process. We should
                    # catch all exceptions and iterate over the results like we do for rets
                    # in other functions.
                    LocalProvisioner._tolerate_no_process(e)
        # if self.process:
        #     if hasattr(signal, "SIGTERM"):  # type:ignore[unreachable]
        #         # If available, give preference to signalling the process group over `terminate()`.
        #         try:
        #             await self._send_signal(signal.SIGTERM)
        #             return
        #         except OSError:
        #             pass
        #     try:
        #         self.process.terminate()
        #     except OSError as e:
        #         LocalProvisioner._tolerate_no_process(e)

    @staticmethod
    def _tolerate_no_process(os_error: OSError) -> None:
        # In Windows, we will get an Access Denied error if the process
        # has already terminated. Ignore it.
        if sys.platform == "win32":
            if os_error.winerror != 5:
                raise
        # On Unix, we may get an ESRCH error (or ProcessLookupError instance) if
        # the process has already terminated. Ignore it.
        else:
            from errno import ESRCH

            if not isinstance(os_error, ProcessLookupError) or os_error.errno != ESRCH:
                raise

    async def cleanup(self, restart: bool = False) -> None:
        """Clean up the resources used by the provisioner and optionally restart."""
        self.parent.log.info("PROXY::CLOSE")
        if self.ports_cached and not restart:
            # provisioner is about to be destroyed, return cached ports
            lpc = LocalPortCache.instance()
            ports = (
                self.connection_info["shell_port"],
                self.connection_info["iopub_port"],
                self.connection_info["stdin_port"],
                self.connection_info["hb_port"],
                self.connection_info["control_port"],
            )
            for port in ports:
                if TYPE_CHECKING:
                    assert isinstance(port, int)
                lpc.return_port(port)

    async def pre_launch(self, **kwargs: Any) -> Dict[str, Any]:
        """Perform any steps in preparation for kernel process launch.

        This includes applying additional substitutions to the kernel launch command and env.
        It also includes preparation of launch parameters.

        Returns the updated kwargs.
        """
        self.parent.log.info("provisioner.pre_launch")
        # This should be considered temporary until a better division of labor can be defined.
        km = self.parent
        if km:
            self.parent.log.info("provisioner.pre_launch km")
            if km.transport == "tcp" and not is_local_ip(km.ip):
                msg = (
                    "Can only launch a kernel on a local interface. "
                    f"This one is not: {km.ip}."
                    "Make sure that the '*_address' attributes are "
                    "configured properly. "
                    f"Currently valid addresses are: {local_ips()}"
                )
                raise RuntimeError(msg)
            # build the Popen cmd
            extra_arguments = kwargs.pop("extra_arguments", [])

            # write connection file / get default ports
            # TODO - change when handshake pattern is adopted
            # NOTE: defauls to true for tcp transport (see manager.py)
            if km.cache_ports and not self.ports_cached:
                lpc = LocalPortCache.instance()
                km.shell_port = lpc.find_available_port(km.ip)
                km.iopub_port = lpc.find_available_port(km.ip)
                km.stdin_port = lpc.find_available_port(km.ip)
                km.hb_port = lpc.find_available_port(km.ip)
                km.control_port = lpc.find_available_port(km.ip)
                self.ports_cached = True
            if "env" in kwargs:
                self.parent.log.info("provisioner.pre_launch env")
                jupyter_session = kwargs["env"].get("JPY_SESSION_NAME", "")
                km.write_connection_file(jupyter_session=jupyter_session)
            else:
                self.parent.log.info("provisioner.pre_launch write")
                km.write_connection_file()
            self.connection_info = km.get_connection_info()

            kernel_cmd = km.format_kernel_cmd(
                extra_arguments=extra_arguments
            )  # This needs to remain here for b/c
        else:
            extra_arguments = kwargs.pop("extra_arguments", [])
            kernel_cmd = self.kernel_spec.argv + extra_arguments

        return await super().pre_launch(cmd=kernel_cmd, **kwargs)

    async def launch_kernel(self, cmd: List[str], **kwargs: Any) -> KernelConnectionInfo:
        """Launch a kernel with a command."""
        self.parent.log.info(f"provisioner.launch_kernel, config: {self.connection_info}")
        scrubbed_kwargs = LocalProvisioner._scrub_kwargs(kwargs)
        # In a prod solution, this functin would take care of launching the proxy then the kernel.
        # For local testing, we should create a LocalKernelProviioner and a LocalLineageProxyProvisioner
        # and re-factor the LocalProvisioner to manage both of them.
        self.parent.log.info("PROXY::LAUNCH")
        self.processes[self.PROXY] = launch_proxy(cmd = ["/bin/sleep", "15"], **scrubbed_kwargs)
        self.processes[self.KERNEL] = launch_kernel(cmd, **scrubbed_kwargs)
        self.parent.log.info(f"provisioner.launch_kernel, config: {self.connection_info}")
        pgid = None
        if hasattr(os, "getpgid"):
            try:
                #pgid = os.getpgid(self.process.pid)
                pgid = os.getpgid(self.processes[self.KERNEL].pid)
            except OSError:
                pass

        self.pids[self.KERNEL] = self.processes[self.KERNEL].pid
        self.pgids[self.KERNEL] = pgid
        # self.pid = self.process.pid
        # self.pgid = pgid
        self.parent.log.info(f"after provisioner.launch_kernel, config: {self.connection_info}")
        return self.connection_info

    @staticmethod
    def _scrub_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Remove any keyword arguments that Popen does not tolerate."""
        keywords_to_scrub: List[str] = ["extra_arguments", "kernel_id"]
        scrubbed_kwargs = kwargs.copy()
        for kw in keywords_to_scrub:
            scrubbed_kwargs.pop(kw, None)
        return scrubbed_kwargs

    async def get_provisioner_info(self) -> Dict:
        """Captures the base information necessary for persistence relative to this instance."""
        provisioner_info = await super().get_provisioner_info()
        #provisioner_info.update({"pid": self.pid, "pgid": self.pgid, "ip": self.ip})
        provisioner_info.update({"pid": self.pids[self.KERNEL], "pgid": self.pgids[self.KERNEL], "ip": self.ip})
        return provisioner_info

    async def load_provisioner_info(self, provisioner_info: Dict) -> None:
        """Loads the base information necessary for persistence relative to this instance."""
        await super().load_provisioner_info(provisioner_info)
        self.pids[self.KERNEL] =  provisioner_info["pid"]
        self.pgids[self.KERNEL] = provisioner_info["pgid"]
        # self.pid = provisioner_info["pid"]
        # self.pgid = provisioner_info["pgid"]
        self.ip = provisioner_info["ip"]
