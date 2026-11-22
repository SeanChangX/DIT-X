# docker buildx bake -f docker/docker-bake.hcl [target]
# Normally driven by `ditx image` / CI, which set the variables below.

variable "REGISTRY"   { default = "ghcr.io/seanchangx" }
variable "TAG"        { default = "local" }
variable "ROS_DISTRO" { default = "humble" }
variable "PLATFORMS"  { default = "linux/amd64" }
# space separated, filled from ditx.repos by `ditx image`
variable "DOMAINS"    { default = "main navigation localization vision" }
# extra tags, space separated (e.g. "stable sha-1a2b3c4")
variable "EXTRA_TAGS" { default = "" }
# commit the images are built from (OCI revision label), set by `ditx image`
variable "REVISION"   { default = "" }

function "tags" {
  params = [name]
  result = concat(
    ["${REGISTRY}/${name}:${TAG}"],
    [for t in compact(split(" ", EXTRA_TAGS)) : "${REGISTRY}/${name}:${t}"])
}

# OCI labels on every image: where it comes from and which commit, as `docker inspect` and the
# GHCR package page show them (https://github.com/opencontainers/image-spec/blob/main/annotations.md)
function "labels" {
  params = [title, description]
  result = {
    "org.opencontainers.image.title"       = title
    "org.opencontainers.image.description" = description
    "org.opencontainers.image.source"      = "https://github.com/SeanChangX/DIT-X"
    "org.opencontainers.image.url"         = "https://seanchangx.github.io/DIT-X/"
    "org.opencontainers.image.licenses"    = "MIT"
    "org.opencontainers.image.version"     = TAG
    "org.opencontainers.image.revision"    = REVISION
  }
}

target "_common" {
  context    = "."
  dockerfile = "docker/Dockerfile"
  platforms  = split(",", PLATFORMS)
  args       = { ROS_DISTRO = ROS_DISTRO }
}

group "default" {
  targets = ["base", "dev", "cross", "runtime"]
}

target "base" {
  inherits = ["_common"]
  target   = "base"
  tags     = tags("eurobot-base")
  labels   = labels("eurobot-base", "ROS 2 ${ROS_DISTRO} + the shared interfaces (base/): what every robot container starts from")
}

target "dev" {
  inherits = ["_common"]
  target   = "dev"
  tags     = tags("eurobot-dev")
  labels   = labels("eurobot-dev", "Build, test and lint environment of DIT-X (ditx build/test/lint, VS Code dev container)")
}

# arm64 cross compile environment; only built for (and used on) amd64 machines
target "cross" {
  inherits  = ["_common"]
  target    = "cross"
  platforms = ["linux/amd64"]
  tags      = tags("eurobot-cross")
  labels    = labels("eurobot-cross", "dev + aarch64 toolchain and arm64 sysroot: ditx build --arch arm64 on amd64, no qemu")
}

target "runtime" {
  name     = "runtime-${d}"
  matrix   = { d = compact(split(" ", DOMAINS)) }
  inherits = ["_common"]
  target   = "runtime"
  args     = { ROS_DISTRO = ROS_DISTRO, DOMAIN = d }
  tags     = tags("eurobot-${d}")
  labels   = labels("eurobot-${d}", "Runtime image of the ${d} domain: what a robot runs (ditx up / deploy)")
}
