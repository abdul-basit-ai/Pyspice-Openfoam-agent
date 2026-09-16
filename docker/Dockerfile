FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV FOAM_BASH=/usr/lib/openfoam/openfoam2406/etc/bashrc

# System utilities, Python, and ngspice shared libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    gnupg \
    build-essential \
    git \
    python3 \
    python3-pip \
    python3-dev \
    ngspice \
    libngspice0 \
    libngspice0-dev \
    libgl1 \
    libglx-mesa0 \
    libosmesa6 \
    libegl1 \
    && rm -rf /var/lib/apt/lists/*

# Symlink so PySpice reliably discovers libngspice
RUN ln -sf /usr/lib/x86_64-linux-gnu/libngspice.so.0 /usr/lib/libngspice.so || true

# Install OpenCFD OpenFOAM v2406 repository and binaries
RUN wget -q -O - https://dl.openfoam.com/add-debian-repo.sh | bash && \
    apt-get update && apt-get install -y --no-install-recommends \
    openfoam2406-default \
    && rm -rf /var/lib/apt/lists/*

# Source OpenFOAM in non-login and login shells
RUN echo "source ${FOAM_BASH}" >> /etc/bash.bashrc && \
    echo "source ${FOAM_BASH}" >> /root/.bashrc

WORKDIR /workspace

# Install core build tooling
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

# Install package (editable: compose mounts the repo over /workspace, so
# code edits take effect without rebuild; src/ is copied so a bare
# `docker run` image also has a working install)
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip3 install --no-cache-dir -e . && python3 -c "import pyspice_openfoam_agent; import PySpice, pyvista, langgraph  # foamlib was a ghost dep, removed 2026-09; print('imports OK')"

# Entrypoint automatically initializes the OpenFOAM environment
ENTRYPOINT ["/bin/bash", "-c", "source /usr/lib/openfoam/openfoam2406/etc/bashrc && exec \"$@\"", "--"]
CMD ["/bin/bash"]