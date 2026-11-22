# syntax=docker/dockerfile:1.7
##############################################################################
# Eurobot images. One Dockerfile, layered so that every domain shares the
# exact same base (ROS + eurobot_interfaces):
#
#   ros ─┬─ toolchain ─┬─ base-build ──────┐
#        │             └─ dev  <───────────┤  (all domains' build deps)
#        │                 └─ cross        │  (amd64 only: + arm64 sysroot, no qemu)
#        └─ base  <────────────────────────┘  (ROS + interfaces, runtime only)
#                 └─ runtime (one per domain: exec deps + that domain's install)
#
# Build with `ditx image` (docker buildx bake, see docker-bake.hcl).
##############################################################################
ARG ROS_DISTRO=humble

########################## ros: common runtime bits ##########################
FROM ros:${ROS_DISTRO}-ros-base AS ros
ARG ROS_DISTRO
ENV ROS_DISTRO=${ROS_DISTRO} \
    LANG=C.UTF-8 \
    TZ=Asia/Taipei \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    CYCLONEDDS_URI=file:///opt/eurobot/cyclonedds.xml \
    RCUTILS_CONSOLE_OUTPUT_FORMAT="[{severity}] [{time}] [{name}]: {message}"
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update && apt-get install -y --no-install-recommends \
      ros-${ROS_DISTRO}-rmw-cyclonedds-cpp tzdata \
 && rosdep update --rosdistro ${ROS_DISTRO} \
 # a copy any user can read: ditx runs containers as your own uid, whose HOME has no cache
 && mkdir -p /etc/ros/rosdep-home && cp -r /root/.ros/rosdep /etc/ros/rosdep-home/ \
 && chmod -R a+rX /etc/ros/rosdep-home \
 # noble images ship an "ubuntu" user on uid 1000; replace it with ours
 && (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u 1000 -s /bin/bash dit
COPY docker/cyclonedds.xml docker/cyclonedds-localhost.xml /opt/eurobot/
COPY docker/entrypoint.sh /opt/eurobot/entrypoint.sh
ENTRYPOINT ["/opt/eurobot/entrypoint.sh"]
CMD ["bash"]

############## toolchain: compilers, linters, language servers ##############
FROM ros AS toolchain
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential ccache clangd gdb git python3-pip python3-pytest shellcheck sudo \
      ros-${ROS_DISTRO}-ament-cmake-gtest ros-${ROS_DISTRO}-ament-cmake-pytest \
      ros-${ROS_DISTRO}-launch-testing-ament-cmake ros-${ROS_DISTRO}-launch-testing-ros \
 # pinned so every distro/machine formats identically (keep in sync with .pre-commit-config.yaml)
 && pip3 install --no-cache-dir $(pip3 help install | grep -q break-system-packages && echo --break-system-packages) \
      clang-format==18.1.8 ruff==0.6.9 \
 && echo "dit ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dit
ENV CCACHE_DIR=/ccache

####################### base-build: compile shared pkgs #######################
FROM toolchain AS base-build
COPY base /src/base
COPY docker/fingerprint.sh /opt/eurobot/fingerprint.sh
RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
 && colcon --log-base /tmp/log build --base-paths /src/base --build-base /tmp/build \
      --install-base /opt/eurobot/base/install --merge-install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
 # fingerprint of the interface definitions: overlays built against a different
 # base are refused at runtime (docker/entrypoint.sh) instead of crashing later
 && /opt/eurobot/fingerprint.sh /src/base > /opt/eurobot/base/FINGERPRINT

######################## base: what every robot runs on ########################
FROM ros AS base
COPY --from=base-build /opt/eurobot/base /opt/eurobot/base
LABEL org.opencontainers.image.source="https://github.com/SeanChangX/DIT-X"
USER dit

#################### manifests: only package.xml of domains ####################
# Copying just the package.xml files keeps the dependency layers below cached
# until someone actually changes a dependency.
FROM ros AS manifests
COPY domains /tmp/domains
RUN find /tmp/domains -name COLCON_IGNORE -printf '%h\0' | xargs -0r rm -rf \
 && find /tmp/domains -type f ! -name package.xml -delete \
 && find /tmp/domains -depth -type d -empty -delete \
 && mkdir -p /tmp/domains

############## dev: everything needed to build and test any domain ##############
FROM toolchain AS dev
COPY --from=base-build /opt/eurobot/base /opt/eurobot/base
COPY --from=manifests /tmp/domains /tmp/manifests
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && source /opt/ros/${ROS_DISTRO}/setup.bash && source /opt/eurobot/base/install/setup.bash \
 && rosdep install -y --from-paths /tmp/manifests --ignore-src --rosdistro ${ROS_DISTRO} \
 && rm -rf /tmp/manifests \
 && mkdir -p /ccache && chmod 777 /ccache
USER dit

########## cross: `ditx build --arch arm64` on an amd64 machine, no qemu ##########
# The dev image plus an aarch64 compiler and an arm64 sysroot (ROS, the build deps
# of base/ and every domain, base/ itself), unpacked from arm64 packages without
# running them. CMAKE_TOOLCHAIN_FILE makes every colcon build here produce arm64.
FROM dev AS cross
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      g++-aarch64-linux-gnu mmdebstrap
COPY docker/cross /opt/eurobot/cross
COPY docker/fingerprint.sh /opt/eurobot/fingerprint.sh
COPY --from=manifests /tmp/domains /tmp/manifests
COPY base /src/base
RUN /opt/eurobot/cross/make-sysroot.sh /opt/eurobot/cross/sysroot /src/base /tmp/manifests \
 && rm -rf /tmp/manifests
ENV CMAKE_TOOLCHAIN_FILE=/opt/eurobot/cross/aarch64.cmake
RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
 && base=/opt/eurobot/cross/sysroot/opt/eurobot/base \
 && colcon --log-base /tmp/log build --base-paths /src/base --build-base /tmp/build \
      --install-base $base/install --merge-install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
 && /opt/eurobot/fingerprint.sh /src/base > $base/FINGERPRINT \
 && rm -rf /tmp/build /tmp/log /src/base
USER dit

############# domain-build / runtime: one image per domain (ARG DOMAIN) #############
FROM dev AS domain-build
ARG DOMAIN
COPY --chown=ditx domains/${DOMAIN} /src/${DOMAIN}
RUN source /opt/ros/${ROS_DISTRO}/setup.bash && source /opt/eurobot/base/install/setup.bash \
 && sudo mkdir -p /opt/eurobot/${DOMAIN} && sudo chown dit /opt/eurobot/${DOMAIN} \
 && colcon --log-base /tmp/log build --base-paths /src/${DOMAIN} --build-base /tmp/build \
      --install-base /opt/eurobot/${DOMAIN}/install --merge-install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF

FROM base AS runtime
ARG DOMAIN
USER root
COPY --from=manifests /tmp/domains/${DOMAIN} /tmp/manifests
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && source /opt/ros/${ROS_DISTRO}/setup.bash && source /opt/eurobot/base/install/setup.bash \
 && rosdep install -y --from-paths /tmp/manifests --ignore-src --rosdistro ${ROS_DISTRO} \
      --dependency-types exec \
 && rm -rf /tmp/manifests
COPY --from=domain-build /opt/eurobot/${DOMAIN} /opt/eurobot/${DOMAIN}
ENV EUROBOT_DOMAIN=${DOMAIN}
USER dit
# Convention: every domain ships <domain>_bringup/launch/bringup.launch.py
CMD ["bash", "-c", "exec ros2 launch ${EUROBOT_DOMAIN}_bringup bringup.launch.py robot:=${ROBOT:-sim}"]
