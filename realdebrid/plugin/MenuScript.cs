using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>Registers the menu step below and the library scanner when Jellyfin starts.</summary>
public class ServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddHttpClient();
        serviceCollection.AddTransient<IStartupFilter, MenuScriptFilter>();
        serviceCollection.AddHostedService<LibraryScanner>();
    }
}

/// <summary>Plugins have no way into Jellyfin's main menu, so its home page gets one extra script tag as it goes out, which adds the entry.</summary>
public class MenuScriptFilter : IStartupFilter
{
    private const string Tag = "<script src=\"../RealDebrid/menu.js\" defer></script>";

    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) => app =>
    {
        app.Use(AddTag);
        next(app);
    };

    private static async Task AddTag(HttpContext context, Func<Task> next)
    {
        var path = context.Request.Path.Value ?? string.Empty;
        var isHome = path.EndsWith("/web/", StringComparison.OrdinalIgnoreCase) || path.EndsWith("/web/index.html", StringComparison.OrdinalIgnoreCase);
        if (!isHome || !HttpMethods.IsGet(context.Request.Method))
        {
            await next().ConfigureAwait(false);
            return;
        }

        // Ask for the plain, full page, so there is something to edit.
        context.Request.Headers.AcceptEncoding = default;
        context.Request.Headers.IfNoneMatch = default;
        context.Request.Headers.IfModifiedSince = default;

        var original = context.Response.Body;
        using var buffer = new MemoryStream();
        context.Response.Body = buffer;
        try
        {
            await next().ConfigureAwait(false);
        }
        finally
        {
            context.Response.Body = original;
        }

        var html = context.Response.ContentType?.StartsWith("text/html", StringComparison.OrdinalIgnoreCase) == true && context.Response.StatusCode == 200;
        var bytes = buffer.ToArray();
        if (html)
        {
            var text = Encoding.UTF8.GetString(bytes);
            var at = text.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
            if (at >= 0)
            {
                bytes = Encoding.UTF8.GetBytes(text.Insert(at, Tag));
            }

            context.Response.Headers.ETag = default;
            context.Response.Headers.LastModified = default;
            context.Response.Headers.AcceptRanges = default;
        }

        context.Response.ContentLength = bytes.Length;
        await original.WriteAsync(bytes).ConfigureAwait(false);
    }
}
