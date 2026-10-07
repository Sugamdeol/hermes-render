(function () {
  "use strict";
  const sdk = window.__HERMES_PLUGIN_SDK__;
  if (!sdk) return;
  const h = sdk.React.createElement;
  function DebugMaster() {
    const [snapshot, setSnapshot] = sdk.hooks.useState("");
    const [busy, setBusy] = sdk.hooks.useState(false);
    const [error, setError] = sdk.hooks.useState("");
    const alive = sdk.hooks.useRef(true);
    async function refresh() {
      setBusy(true); setError("");
      try {
        const data = await sdk.fetchJSON("/api/plugins/debug-master/snapshot");
        if (alive.current) setSnapshot(JSON.stringify(data, null, 2));
      } catch (e) {
        if (alive.current) setError("Snapshot could not load: " + e.message);
      } finally { if (alive.current) setBusy(false); }
    }
    sdk.hooks.useEffect(function () {
      alive.current = true; refresh();
      return function () { alive.current = false; };
    }, []);
    return h("section", {style:{padding:"24px",maxWidth:"900px",margin:"0 auto"}},
      h("h2", null, "Debug snapshot"),
      h("button", {onClick:refresh,disabled:busy,type:"button"}, busy ? "Loading…" : "Refresh snapshot"),
      error ? h("p", {role:"alert"}, error) : null,
      h("textarea", {readOnly:true,value:snapshot,"aria-label":"Debug snapshot",
        style:{width:"100%",minHeight:"400px",marginTop:"16px",fontFamily:"monospace"}}));
  }
  window.__HERMES_PLUGINS__.register("debug-master", DebugMaster);
})();
