# Dodam benchmark image for C# on .NET: ASP.NET Core minimal APIs and Microsoft.Data.Sqlite, plus the judge.
# Build from bench/images: docker build -f csharp/Dockerfile -t dodam-bench/csharp:dev .
FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:de4fe90a98845d75910b9769662465c84de1a934665c431279d1382b2bddc02b

# The SDK sends no telemetry and checks for no workload updates, so nothing reaches for the network.
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY common/setup.sh /opt/dodam/setup.sh
COPY judge/requirements.txt /opt/dodam/judge-requirements.txt
RUN /opt/dodam/setup.sh judge

COPY csharp/app/ /app/
# The base image's own uid-1000 user, ubuntu, is in groups such as sudo and adm; agent starts without them.
RUN userdel --remove ubuntu && /opt/dodam/setup.sh agent

# The user agent restores the pinned packages once, in locked mode, into its NuGet folder
# (~/.nuget/packages); the lock file holds every version and content hash. /app/obj keeps the restore
# output that the build reads with --no-restore. A judged build never fetches a package, and the
# verifier's fresh container holds only these packages, whatever the agent restored in its own.
WORKDIR /app
USER agent
RUN dotnet restore --locked-mode && dotnet nuget locals http-cache --clear
USER root
