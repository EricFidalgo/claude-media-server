using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>The only way in to the realdebrid service from outside Docker, and only for Jellyfin admins.</summary>
[ApiController]
[ApiExplorerSettings(IgnoreApi = true)]
[Route("RealDebrid")]
public class RealDebridController : ControllerBase
{
    internal static readonly Uri Service = new(Environment.GetEnvironmentVariable("REALDEBRID_SERVICE") ?? "http://realdebrid:8097/");
    private readonly IHttpClientFactory _clients;

    public RealDebridController(IHttpClientFactory clients)
    {
        _clients = clients;
    }

    /// <summary>The script that adds the menu entry. It has to load before login, and holds nothing private.</summary>
    [HttpGet("menu.js")]
    [AllowAnonymous]
    public IActionResult Menu()
    {
        var script = typeof(Plugin).Assembly.GetManifestResourceStream("RealDebrid.menu.js")!;
        Response.Headers.CacheControl = "no-cache";
        return File(script, "text/javascript");
    }

    /// <summary>The page itself. It stands alone, outside Jellyfin's dashboard. It holds nothing private, and everything it shows comes through Forward below.</summary>
    [HttpGet("app")]
    [AllowAnonymous]
    public IActionResult App()
    {
        var page = typeof(Plugin).Assembly.GetManifestResourceStream("RealDebrid.page.html")!;
        Response.Headers.CacheControl = "no-cache";
        Response.Headers.ContentSecurityPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";
        return File(page, "text/html; charset=utf-8");
    }

    /// <summary>Passes a request from the page to the service, and the answer back.</summary>
    [AcceptVerbs("GET", "POST", "DELETE")]
    [Route("api/{**path}")]
    [Authorize(Policy = "RequiresElevation")]
    public Task<IActionResult> Forward(string path, CancellationToken cancellationToken)
        => ServiceProxy.Forward(Request, _clients, Service, "Real-Debrid", path, cancellationToken);
}
