# repository.verify/v1

Minimal offline validation image for code-review fixtures. It contains Git,
BusyBox, and one deterministic verifier. The image itself does not clone, pull, or
contact a network; `LocalDockerProvider` additionally creates every validation
container with `--network none`, a read-only root filesystem, and a read-only
candidate mount.

The Dockerfile base tag is used only at build time. `aceval environment build`
inspects the resulting image ID and writes that immutable `sha256:...` value to
the generated EnvironmentBlueprint. Evaluation never implicitly pulls or runs
the mutable tag.
