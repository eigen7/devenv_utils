"""Run several dev containers from sibling clones of the same repo at once.

A user puts ``"INSTANCE": N`` in ``.env.json`` (default 0). Instance N gets:

  * a distinct container name (``<base>`` for instance 0, ``<base>_N`` for
    N > 0) so a second ``run_docker.py`` launches a new container instead of
    exec'ing into the first one, and
  * every forwarded port shifted up by ``INSTANCE_PORT_STRIDE * N`` (the stride
    is configurable via ``DevenvConfig.instance_port_stride``) so the instances
    don't collide on the host.

The same offset is exported into the container as ``INSTANCE_PORT_OFFSET_ENV``
(``DEVENV_INSTANCE_PORT_OFFSET``). In-container apps read that variable and add
it to their default ports, so they bind the ports that are actually forwarded.
This works under both bridge networking (the offset shifts the ``-p`` mappings)
and host networking (no ``-p`` mappings, but the apps still need to bind
non-colliding ports).
"""

from .console import SetupException

# .env.json key holding the instance number.
INSTANCE_ENV_KEY = "INSTANCE"

# Name of the environment variable carrying the computed port offset into the
# container. In-container apps read this and add it to their default ports.
INSTANCE_PORT_OFFSET_ENV = "DEVENV_INSTANCE_PORT_OFFSET"


def instance_number(env: dict) -> int:
    """The instance index from a parsed .env.json mapping (0 when unset)."""
    return int(env.get(INSTANCE_ENV_KEY, 0))


def port_offset(instance: int, stride: int) -> int:
    """How much instance `instance` shifts its ports up by."""
    return instance * stride


def instanced_name(base_name: str, instance: int) -> str:
    """Container name for `instance`: the base name for instance 0, else
    ``<base>_<instance>`` so each instance is a distinct container."""
    return base_name if instance == 0 else f"{base_name}_{instance}"


def assert_no_port_conflicts(base_ports, instance: int, stride: int) -> None:
    """Verify instance `instance`'s forwarded ports don't collide with any of
    instances 0..instance-1, which are all assumed to be running.

    Raises SetupException naming the first clashing port. With sufficiently
    spaced `base_ports` and `stride` this never triggers; it guards against a
    later port addition (or a small stride) that would silently overlap.
    """
    owner = {}  # forwarded port -> instance that forwards it
    for k in range(instance + 1):
        offset = port_offset(k, stride)
        for base in base_ports:
            port = base + offset
            if port in owner:
                raise SetupException(
                    f"INSTANCE port conflict: instance {k} would forward port "
                    f"{port} (base {base} + offset {offset}), which instance "
                    f"{owner[port]} already forwards. Increase the spacing "
                    f"between REQUIRED_PORTS entries or the instance_port_stride "
                    f"(currently {stride}), or lower INSTANCE."
                )
            owner[port] = k
