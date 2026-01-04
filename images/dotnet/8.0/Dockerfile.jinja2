FROM {{ "base" | resolve_base_image }}
USER 0
RUN apt-get update -y \
    && apt-get install -y \
        libc6 \
        libgcc-s1 \
        libicu74 \
        liblttng-ust1 \
        libssl3 \
        libstdc++6 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*
RUN curl -L https://dot.net/v1/dotnet-install.sh -o /usr/bin/install-dotnet \
    && chmod +x /usr/bin/install-dotnet \
    && install-dotnet --channel {{ "dotnet-sdk" | resolve_version}} --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet
USER 1000
