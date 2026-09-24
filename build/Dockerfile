# The definition of a build (ARCHITECTURE.md 12.1).
#
# NativeAOT cannot cross-compile: it invokes the platform linker and links against the platform's
# native libraries, so a linux-x64 binary must be produced on Linux. This file is why there is no
# "works on my machine" path.
#
#   docker build -f build/Dockerfile -t msgraph-core-build .
#   docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest" msgraph-core-build \
#          cp /out/libMicrosoftGraph.so /dest/

FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build

# The NativeAOT linker prerequisites. Without these the ILC step succeeds and the link fails.
RUN apt-get update \
 && apt-get install -y --no-install-recommends clang zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Restore on the manifests alone, so a source-only change does not re-download the graph.
COPY MicrosoftGraph.slnx ./
COPY src/MicrosoftGraph/MicrosoftGraph.csproj src/MicrosoftGraph/
COPY tests/UnitTests/UnitTests.csproj tests/UnitTests/
COPY tests/IntegrationTests/IntegrationTests.csproj tests/IntegrationTests/
RUN dotnet restore src/MicrosoftGraph/MicrosoftGraph.csproj -r linux-x64

COPY . .

# The managed suite runs here too, so the artefact is never built from code that does not pass.
RUN dotnet test --nologo

RUN dotnet publish src/MicrosoftGraph -r linux-x64 -c Release -o /out \
 && test -f /out/libMicrosoftGraph.so

# Keep the image small: the build output is the point, not the SDK.
FROM debian:trixie-slim AS artifact
COPY --from=build /out/libMicrosoftGraph.so /out/libMicrosoftGraph.so
CMD ["true"]
