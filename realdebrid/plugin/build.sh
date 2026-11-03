#!/bin/sh
# Builds the plugin with a throwaway .NET container (the machine has no .NET installed) and installs it into
#   $1, a Jellyfin config folder (default: the real one). Restart Jellyfin afterwards.
# Rebuild when Jellyfin moves to a new major version and drops the plugin: bump 12.4.0 here and in the .csproj.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
config=${1:-$here/../../jellyfin/config}
version=1.0.0.0
abi=12.4.0.0

docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e NUGET_PACKAGES=/src/.build/nuget \
    -v "$here:/src" -w /src mcr.microsoft.com/dotnet/sdk:10.0 \
    dotnet publish -c Release -o .build/out --nologo -v quiet -p:UseAppHost=false Jellyfin.Plugin.RealDebrid.csproj

target=$config/plugins/RealDebrid_$version
mkdir -p "$target"
cp "$here/.build/out/Jellyfin.Plugin.RealDebrid.dll" "$target/"
cat > "$target/meta.json" <<EOF
{
  "category": "General",
  "changelog": "",
  "description": "Search your Real-Debrid videos and download them to the library.",
  "guid": "7c1f6a52-3d4e-4b8a-9e21-5a0c8d6f2b13",
  "name": "Real-Debrid",
  "overview": "Real-Debrid inside Jellyfin",
  "owner": "local",
  "targetAbi": "$abi",
  "timestamp": "2026-11-02T00:00:00.0000000Z",
  "version": "$version",
  "status": "Active",
  "autoUpdate": false,
  "assemblies": ["Jellyfin.Plugin.RealDebrid.dll"]
}
EOF
# Rebuildable and large, so it stays out of the nightly backups.
rm -rf "$here/.build" "$here/bin" "$here/obj"
echo "Installed to $target"
