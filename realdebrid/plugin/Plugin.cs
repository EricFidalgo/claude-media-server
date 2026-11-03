using System;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>Real-Debrid's page and pass-through, served by Jellyfin. The work and the token live in the realdebrid service next to it.</summary>
public class Plugin : BasePlugin<BasePluginConfiguration>
{
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
    }

    public override string Name => "Real-Debrid";

    public override Guid Id => Guid.Parse("7c1f6a52-3d4e-4b8a-9e21-5a0c8d6f2b13");
}
