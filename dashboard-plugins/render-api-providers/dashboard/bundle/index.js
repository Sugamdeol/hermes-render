/* render-api-providers — dashboard plugin bundle.
 *
 * Adds a "Custom API providers" card to the top of the Models page so a new
 * OpenAI/Anthropic-compatible API provider can be added from the dashboard
 * instead of hand-editing config.yaml.
 *
 * Plain IIFE — no build step. React and UI primitives come from the Plugin
 * SDK (window.__HERMES_PLUGIN_SDK__); see the upstream "Extending the
 * Dashboard" docs. Styling beyond common Tailwind utilities lives in
 * bundle/style.css (plugin bundles are loaded at runtime, so Tailwind does
 * not compile classes that only appear here).
 *
 * Note: upstream's docs use "dist/" for plugin bundles; this repo uses
 * "bundle/" so the files are not dropped by tooling that prunes common
 * build-output directory names. The manifest's "entry" and "css" fields
 * point here.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return; // dashboard not ready; App will not be asking for us

  var h = SDK.React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components;
  var Card = C.Card;
  var CardHeader = C.CardHeader;
  var CardTitle = C.CardTitle;
  var CardContent = C.CardContent;
  var Badge = C.Badge;
  var Button = C.Button;
  var Input = C.Input;
  var Label = C.Label;
  var Select = C.Select;
  var SelectOption = C.SelectOption;
  var fetchJSON = SDK.fetchJSON;
  var hermesApi = SDK.api;

  var PLUGIN = "render-api-providers";
  var PLUGIN_BASE = "/api/plugins/" + PLUGIN;
  var API_BASE = "/api/plugins/" + PLUGIN + "/custom-providers";

  var API_MODES = [
    { value: "", label: "auto (detect from URL)" },
    { value: "chat_completions", label: "OpenAI chat completions" },
    { value: "anthropic_messages", label: "Anthropic messages" },
  ];

  // ── helpers ────────────────────────────────────────────────────────────

  function apiErrorMessage(err) {
    var msg = err instanceof Error ? err.message : String(err);
    // fetchJSON reports "<status>: <body>"; FastAPI bodies carry a detail.
    var body = msg.includes(": ") ? msg.slice(msg.indexOf(": ") + 2) : msg;
    try {
      var parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (Array.isArray(parsed) && parsed[0] && typeof parsed[0].msg === "string") {
        return parsed[0].msg;
      }
    } catch (e) {
      /* not JSON — fall through */
    }
    return msg;
  }

  function normalizeKey(value) {
    return String(value || "").trim().toLowerCase().replace(/\s+/g, "-");
  }

  function isMainProvider(entry, mainProvider) {
    if (!mainProvider) return false;
    var lower = mainProvider.toLowerCase();
    if (lower.indexOf("custom:") !== 0) return false;
    // Normalize the referenced name the way the runtime does
    // (lowercase, spaces → hyphens) so display-name references match.
    var suffix = lower.slice("custom:".length).trim().replace(/\s+/g, "-");
    return suffix === normalizeKey(entry.key) || suffix === normalizeKey(entry.name);
  }

  // ── provider form (add + edit) ─────────────────────────────────────────

  function ProviderForm(props) {
    var initial = props.initial || null; // existing entry when editing
    var editing = !!initial;

    // Multi-key support: track apiKeys as a list
    var initialKeys = (initial && initial.api_keys) ? initial.api_keys.slice() : [];
    var initialApiKey = (initial && initial.api_key) ? initial.api_key : "";
    // Use api_keys list if present, otherwise fall back to single api_key
    var effectiveInitialKeys = initialKeys.length > 0 ? initialKeys : [initialApiKey].filter(Boolean);

    var state = useState({
      name: initial ? initial.name : "",
      baseUrl: initial ? initial.base_url : "",
      apiKeys: effectiveInitialKeys.slice(),
      clearApiKeys: false,
      keyEnv: initial ? initial.key_env : "",
      apiMode: initial ? initial.api_mode : "",
      model: initial ? initial.model : "",
      setAsMain: false,
      busy: false,
      error: null,
    });
    var form = state[0];
    var setForm = state[1];

    var set = useCallback(
      function (patch) {
        setForm(function (prev) {
          var next = {};
          for (var k in prev) next[k] = prev[k];
          for (var p in patch) next[p] = patch[p];
          return next;
        });
      },
      [setForm],
    );

    var addKey = useCallback(function () {
      setForm(function (prev) {
        var next = {};
        for (var k in prev) next[k] = prev[k];
        next.apiKeys = prev.apiKeys.slice();
        next.apiKeys.push("");
        return next;
      });
    }, [setForm]);

    var removeKey = useCallback(function (index) {
      setForm(function (prev) {
        var next = {};
        for (var k in prev) next[k] = prev[k];
        next.apiKeys = prev.apiKeys.slice();
        next.apiKeys.splice(index, 1);
        return next;
      });
    }, [setForm]);

    var updateKey = useCallback(function (index, value) {
      setForm(function (prev) {
        var next = {};
        for (var k in prev) next[k] = prev[k];
        next.apiKeys = prev.apiKeys.slice();
        next.apiKeys[index] = value;
        return next;
      });
    }, [setForm]);

    var validate = function () {
      if (!editing && !form.name.trim()) return "Name is required";
      var parsedUrl;
      try {
        parsedUrl = new URL(form.baseUrl.trim());
      } catch (e) {
        return "Base URL must be a valid http(s):// address with a host";
      }
      if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
        return "Base URL must be http or https";
      }
      if (form.keyEnv.trim() && !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(form.keyEnv.trim())) {
        return "Key env var must be a valid environment variable name";
      }
      // Validate multi-keys: at most 10 non-empty keys
      var nonEmptyKeys = form.apiKeys.filter(function (k) { return k.trim() !== ""; });
      if (nonEmptyKeys.length > 10) {
        return "At most 10 API keys are allowed";
      }
      return null;
    };

    var submit = async function (e) {
      if (e && e.preventDefault) e.preventDefault();
      var problem = validate();
      if (problem) {
        set({ error: problem });
        return;
      }
      // Build payload: use api_keys list if multiple keys, otherwise single api_key
      var payload = {
        name: form.name.trim(),
        base_url: form.baseUrl.trim().replace(/\/+$/, ""),
        api_mode: form.apiMode,
        key_env: form.keyEnv.trim(),
        model: form.model.trim(),
        clear_api_key: form.clearApiKeys,
      };
      // Filter to non-empty keys for the payload
      var nonEmptyKeys = form.apiKeys.filter(function (k) { return k.trim() !== ""; });
      if (nonEmptyKeys.length > 0) {
        if (nonEmptyKeys.length === 1) {
          // Single key — use legacy api_key field for backwards compat
          payload.api_key = nonEmptyKeys[0].trim();
        } else {
          // Multiple keys — use new api_keys list
          payload.api_keys = nonEmptyKeys.map(function (k) { return k.trim(); });
        }
      }

      set({ busy: true, error: null });
      try {
        var resp = await fetchJSON(API_BASE, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        var message = editing
          ? "Updated provider '" + resp.name + "'."
          : "Added provider '" + resp.name + "' — new sessions can use " + resp.provider_ref + ".";
        if (!editing && form.setAsMain && form.model.trim()) {
          await hermesApi.setModelAssignment({
            scope: "main",
            provider: resp.provider_ref,
            model: form.model.trim(),
          });
          message += " Main model set to " + form.model.trim() + " on " + resp.name + ".";
        } else if (!editing && form.setAsMain && !form.model.trim()) {
          message +=
            " Set a default model (or use 'Use as' on a model card) to make it the main model.";
        }
        props.onDone(message, "ok");
      } catch (err) {
        set({ busy: false, error: apiErrorMessage(err) });
      }
    };

    return h(
      "form",
      {
        className: "rapi-form",
        onSubmit: submit,
        noValidate: true,
      },
      h(
        "div",
        { className: "rapi-field" },
        h(Label, { htmlFor: "rapi-name" }, editing ? "Name" : "Name *"),
        h(Input, {
          id: "rapi-name",
          value: form.name,
          disabled: editing,
          placeholder: "e.g. together, my-local-llm",
          maxLength: 64,
          onChange: function (e) { set({ name: e.target.value }); },
        }),
      ),
      h(
        "div",
        { className: "rapi-field" },
        h(Label, { htmlFor: "rapi-url" }, "Base URL *"),
        h(Input, {
          id: "rapi-url",
          value: form.baseUrl,
          placeholder: "https://api.example.com/v1",
          onChange: function (e) { set({ baseUrl: e.target.value }); },
        }),
      ),
      h(
        "div",
        { className: "rapi-field", style: { gridColumn: "1 / -1" } },
        h(Label, { htmlFor: "rapi-keys-label" }, "API keys"),
        h("p", { className: "rapi-hint", style: { marginBottom: "8px" } },
          "Add multiple keys for rate-limit rotation. If one key hits its limit, another is used. Up to 10 keys supported.",
        ),
        h("div", { className: "rapi-keys-list" },
          form.apiKeys.map(function (key, index) {
            return h(
              "div",
              { className: "rapi-key-row", style: { display: "flex", gap: "8px", alignItems: "center", marginBottom: "8px" } },
              h(Input, {
                id: "rapi-key-" + index,
                type: "password",
                value: key,
                placeholder: "API key " + (index + 1),
                autoComplete: "off",
                onChange: function (e) { updateKey(index, e.target.value); },
                style: { flex: "1" },
              }),
              h(Button, {
                type: "button",
                size: "sm",
                variant: "ghost",
                onClick: function () { removeKey(index); },
                disabled: form.apiKeys.length <= 1,
                title: "Remove this key",
                style: { flexShrink: 0 },
              }, "×"),
            );
          }),
          h(Button, {
            type: "button",
            size: "sm",
            variant: "outline",
            onClick: addKey,
            disabled: form.apiKeys.length >= 10,
          }, "+ Add another key"),
        ),
        editing && initial.has_api_key && !form.clearApiKeys
          ? h(Button, {
              type: "button",
              size: "sm",
              outlined: true,
              onClick: function () { set({ clearApiKeys: true }); },
            }, "Clear all saved keys")
          : null,
        editing && form.clearApiKeys
          ? h("span", { className: "rapi-status rapi-status-err" }, "All saved keys will be removed when you save.")
          : null,
      ),
      h(
        "div",
        { className: "rapi-field" },
        h(Label, { htmlFor: "rapi-env" }, "Key env var"),
        h(Input, {
          id: "rapi-env",
          value: form.keyEnv,
          placeholder: "e.g. MY_PROVIDER_API_KEY",
          onChange: function (e) { set({ keyEnv: e.target.value }); },
        }),
        h("span", { className: "rapi-hint" }, "Env vars set in Render's Environment tab survive restarts; inline keys live only in config.yaml."),
      ),
      h(
        "div",
        { className: "rapi-field" },
        h(Label, { htmlFor: "rapi-mode" }, "API mode"),
        h(
          Select,
          {
            id: "rapi-mode",
            value: form.apiMode,
            onValueChange: function (v) { set({ apiMode: v }); },
          },
          API_MODES.map(function (m) {
            return h(SelectOption, { key: m.value, value: m.value }, m.label);
          }),
        ),
      ),
      h(
        "div",
        { className: "rapi-field" },
        h(Label, { htmlFor: "rapi-model" }, "Default model"),
        h(Input, {
          id: "rapi-model",
          value: form.model,
          placeholder: "Optional — e.g. qwen-3.8-max-free",
          maxLength: 200,
          onChange: function (e) { set({ model: e.target.value }); },
        }),
      ),
      !editing
        ? h(
            "div",
            { className: "rapi-field" },
            h(
              "label",
              { className: "rapi-checkbox", htmlFor: "rapi-main" },
              h(Input, {
                id: "rapi-main",
                type: "checkbox",
                checked: form.setAsMain,
                onChange: function (e) { set({ setAsMain: e.target.checked }); },
              }),
              h("span", null, "Set as main model (needs a default model)"),
            ),
          )
        : null,
      form.error
        ? h("div", { className: "rapi-status rapi-status-err" }, form.error)
        : null,
      h(
        "div",
        { className: "rapi-actions" },
        h(
          Button,
          { type: "button", size: "sm", outlined: true, onClick: props.onCancel, disabled: form.busy },
          "Cancel",
        ),
        h(
          Button,
          {
            type: "submit",
            size: "sm",
            disabled: form.busy,
          },
          form.busy
            ? "Saving…"
            : editing
              ? "Save changes"
              : form.setAsMain
                ? "Add & set as main"
                : "Add provider",
        ),
      ),
    );
  }

  // ── provider row ───────────────────────────────────────────────────────

  function ProviderRow(props) {
    var entry = props.entry;
    var isMain = isMainProvider(entry, props.mainProvider);

    var state = useState({ modelPrompt: false, modelValue: "", busy: false });
    var ui = state[0];
    var setUi = state[1];
    var [error, setError] = useState(null);
    var modelState = useState(entry.models || []);
    var availableModels = modelState[0];
    var setAvailableModels = modelState[1];
    var [modelsFetched, setModelsFetched] = useState(!!entry.models_fetched);

    useEffect(function () {
      setAvailableModels(entry.models || []);
      setModelsFetched(!!entry.models_fetched);
    }, [entry.key, entry.models, entry.models_fetched, setAvailableModels, setModelsFetched]);

    var refreshModels = async function () {
      setError(null);
      try {
        var refreshed = await fetchJSON(API_BASE + "/" + encodeURIComponent(entry.key) + "/models");
        setAvailableModels(refreshed.models || []);
        setModelsFetched(!!refreshed.fetched);
        if (!refreshed.fetched && refreshed.detail) setError(refreshed.detail);
      } catch (err) {
        setError(apiErrorMessage(err));
      }
    };

    var setMain = useCallback(
      async function (model) {
        setUi(function (prev) {
          var next = {};
          for (var k in prev) next[k] = prev[k];
          next.busy = true;
          return next;
        });
        setError(null);
        try {
          await hermesApi.setModelAssignment({
            scope: "main",
            provider: "custom:" + entry.key,
            model: model,
          });
          props.onStatus("Main model set to " + model + " on " + entry.name + ".", "ok");
          setUi(function (prev) {
            var next = {};
            for (var k in prev) next[k] = prev[k];
            next.busy = false;
            next.modelPrompt = false;
            next.modelValue = "";
            return next;
          });
        } catch (err) {
          setError(apiErrorMessage(err));
          setUi(function (prev) {
            var next = {};
            for (var k in prev) next[k] = prev[k];
            next.busy = false;
            return next;
          });
        }
      },
      [entry.key, entry.name, props.onStatus],
    );

    var remove = async function () {
      if (!window.confirm("Remove provider '" + entry.name + "' from config.yaml?")) return;
      setUi(function (prev) {
        var next = {};
        for (var k in prev) next[k] = prev[k];
        next.busy = true;
        return next;
      });
      setError(null);
      try {
        await fetchJSON(API_BASE + "/" + encodeURIComponent(entry.key), {
          method: "DELETE",
        });
        props.onStatus("Removed provider '" + entry.name + "'.", "ok");
      } catch (err) {
        setError(apiErrorMessage(err));
        setUi(function (prev) {
          var next = {};
          for (var k in prev) next[k] = prev[k];
          next.busy = false;
          return next;
        });
      }
    };

    var keyBadge = entry.has_api_key
      ? (entry.api_key_count && entry.api_key_count > 1
          ? entry.api_key_count + " keys (rotation)"
          : "key in config")
      : entry.key_env
        ? "env " + entry.key_env
        : "no key";
    var modelBadge = availableModels.length
      ? (modelsFetched ? "models: " : "configured: ") + availableModels.length
      : "models unavailable";

    return h(
      "div",
      { className: "rapi-root" },
      h(
        "div",
        { className: "rapi-row" + (isMain ? " rapi-row-main" : "") },
        h(
          "div",
          { className: "rapi-row-info" },
          h(
            "div",
            { className: "rapi-row-title" },
            h("span", { className: "text-sm font-medium font-mono truncate" }, entry.name),
            isMain
              ? h(Badge, { tone: "secondary", className: "text-[9px]" }, "main")
              : null,
            h(Badge, { tone: "secondary", className: "rapi-badge" }, keyBadge),
            h(
              Badge,
              { tone: "secondary", className: "rapi-badge" },
              entry.api_mode || "auto",
            ),
            h(Badge, { tone: "secondary", className: "rapi-badge" }, modelBadge),
            entry.model
              ? h(Badge, { tone: "secondary", className: "rapi-badge" }, "model: " + entry.model)
              : null,
          ),
          h(
            "span",
            { className: "rapi-row-url text-xs font-mono text-muted-foreground truncate" },
            entry.base_url,
          ),
        ),
        h(
          "div",
          { className: "rapi-row-actions" },
          !isMain
            ? h(
                Button,
                {
                  type: "button",
                  size: "sm",
                  outlined: true,
                  disabled: ui.busy,
                  onClick: function () {
                    if (entry.model) setMain(entry.model);
                    else setUi(function (prev) {
                      var next = {};
                      for (var k in prev) next[k] = prev[k];
                      next.modelPrompt = true;
                      next.modelValue = availableModels.length ? availableModels[0] : "";
                      return next;
                    });
                  },
                },
                "Set as main",
              )
            : null,
          h(
            Button,
            {
              type: "button",
              size: "sm",
              outlined: true,
              disabled: ui.busy,
              onClick: refreshModels,
            },
            "Test & refresh models",
          ),
          h(
            Button,
            {
              type: "button",
              size: "sm",
              outlined: true,
              disabled: ui.busy,
              onClick: props.onEdit,
            },
            "Edit",
          ),
          h(
            Button,
            {
              type: "button",
              size: "sm",
              outlined: true,
              disabled: ui.busy,
              className: "rapi-remove",
              onClick: remove,
            },
            "Remove",
          ),
        ),
      ),
      ui.modelPrompt
        ? h(
            "div",
            { className: "rapi-model-input" },
            availableModels.length
              ? h(
                  Select,
                  {
                    value: ui.modelValue,
                    onValueChange: function (value) {
                      setUi(function (prev) {
                        var next = {};
                        for (var k in prev) next[k] = prev[k];
                        next.modelValue = value;
                        return next;
                      });
                    },
                  },
                  availableModels.map(function (modelId) {
                    return h(SelectOption, { key: modelId, value: modelId }, modelId);
                  }),
                )
              : h(Input, {
                  value: ui.modelValue,
                  placeholder: "Model ID, e.g. qwen-3.8-max-free",
                  maxLength: 200,
                  onChange: function (e) {
                    setUi(function (prev) {
                      var next = {};
                      for (var k in prev) next[k] = prev[k];
                      next.modelValue = e.target.value;
                      return next;
                    });
                  },
                }),
            h(
              Button,
              {
                type: "button",
                size: "sm",
                disabled: ui.busy || !ui.modelValue.trim(),
                onClick: function () { setMain(ui.modelValue.trim()); },
              },
              "Use model",
            ),
          )
        : null,
      error ? h("div", { className: "rapi-status rapi-status-err" }, error) : null,
    );
  }

  // ── root card ──────────────────────────────────────────────────────────

  function ApiProvidersCard() {
    var [providers, setProviders] = useState([]);
    var [mainProvider, setMainProvider] = useState("");
    var [loading, setLoading] = useState(true);
    var [loadError, setLoadError] = useState(null);
    var [formOpen, setFormOpen] = useState(false);
    var [editing, setEditing] = useState(null);
    var [status, setStatus] = useState(null);
    var [backup, setBackup] = useState(null);
    var [recovery, setRecovery] = useState(null);

    var refreshBackup = useCallback(async function () {
      try {
        setBackup(await fetchJSON(PLUGIN_BASE + "/storage-status"));
      } catch (err) {
        setBackup({ state: "unknown", error: apiErrorMessage(err) });
      }
    }, []);

    var refreshRecovery = useCallback(async function () {
      try {
        setRecovery(await fetchJSON(PLUGIN_BASE + "/recovery-status"));
      } catch (err) {
        setRecovery({ state: "unavailable", pending_count: 0, error: apiErrorMessage(err) });
      }
    }, []);

    var load = useCallback(async function () {
      setLoading(true);
      setLoadError(null);
      try {
        var resp = await fetchJSON(API_BASE);
        setProviders(resp.providers || []);
        setMainProvider(resp.main_provider || "");
      } catch (err) {
        setLoadError(apiErrorMessage(err));
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(function () {
      load();
      refreshBackup();
      refreshRecovery();
    }, [load, refreshBackup, refreshRecovery]);

    var onStatus = useCallback(function (text, kind) {
      setStatus({ text: text, kind: kind || "ok" });
      load();
    }, [load]);

    var openAdd = function () {
      setEditing(null);
      setStatus(null);
      setFormOpen(true);
    };

    var openEdit = function (entry) {
      setEditing(entry);
      setStatus(null);
      setFormOpen(true);
    };

    var formDone = function (message, kind) {
      setFormOpen(false);
      setEditing(null);
      onStatus(message, kind);
    };

    return h(
      Card,
      null,
      h(
        CardHeader,
        { className: "pb-3" },
        h(
          "div",
          { className: "flex items-center justify-between gap-3 flex-wrap" },
          h(
            "div",
            { className: "min-w-0" },
            h(CardTitle, { className: "text-sm" }, "Custom API providers"),
            h(
              "p",
              { className: "text-xs text-muted-foreground mt-1" },
              "OpenAI- or Anthropic-compatible endpoints. Added to config.yaml and available to new sessions as ",
              h("code", { className: "text-[11px]" }, "custom:<name>"),
              ".",
            ),
          ),
          !formOpen
            ? h(Button, { type: "button", size: "sm", onClick: openAdd }, "Add provider")
            : null,
        ),
      ),
      h(
        CardContent,
        { className: "rapi-root pt-3" },
        h(
          "div",
          { className: "rapi-storage" },
          h(
            "div",
            { className: "min-w-0" },
            h("div", { className: "font-medium text-sm" }, "Private GitHub backup"),
            h(
              "div",
              { className: "text-xs text-muted-foreground mt-1" },
              !backup || backup.state === "starting" || backup.state === "pending"
                ? "Waiting for the first successful backup…"
                : backup.state === "not_configured"
                  ? "Set GIT_STATE_REPO and GIT_STATE_TOKEN in Render to enable backups."
                  : backup.state === "ok"
                    ? "Last successful backup: " + (backup.last_success_at || "unknown time")
                    : backup.state === "error"
                      ? "Backup failed: " + (backup.error || "check service logs")
                      : backup.error || "Backup status unavailable.",
            ),
          ),
          h(
            "div",
            { className: "min-w-0" },
            h("div", { className: "font-medium text-sm" }, "Interrupted task recovery"),
            h(
              "div",
              { className: "text-xs text-muted-foreground mt-1" },
              !recovery
                ? "Checking recovery queue…"
                : recovery.state === "unavailable"
                  ? recovery.error || "Recovery status unavailable."
                  : recovery.pending_count === 0
                    ? "No unfinished gateway tasks."
                    : recovery.pending_count + " unfinished session(s) are queued for automatic resume.",
            ),
          ),
          h(
            Button,
            { type: "button", size: "sm", outlined: true, onClick: function () { refreshBackup(); refreshRecovery(); } },
            "Refresh status",
          ),
        ),
        formOpen
          ? h(
              ProviderForm,
              {
                initial: editing,
                onDone: formDone,
                onCancel: function () {
                  setFormOpen(false);
                  setEditing(null);
                },
              },
            )
          : null,
        loading
          ? h("div", { className: "rapi-status" }, "Loading providers…")
          : null,
        loadError
          ? h("div", { className: "rapi-status rapi-status-err" }, loadError)
          : null,
        status
          ? h(
              "div",
              {
                className:
                  "rapi-status " + (status.kind === "err" ? "rapi-status-err" : "rapi-status-ok"),
              },
              status.text,
            )
          : null,
        !loading && !loadError && providers.length === 0 && !formOpen
          ? h(
              "div",
              { className: "rapi-status" },
              "No custom providers yet. Add one above — the boot-time Bynara provider is built into the image and managed by the config patcher, so it will appear here when present.",
            )
          : null,
        !loading &&
          !loadError &&
          providers.map(function (entry) {
            return h(ProviderRow, {
              key: entry.key || entry.name,
              entry: entry,
              mainProvider: mainProvider,
              onEdit: function () { openEdit(entry); },
              onStatus: onStatus,
            });
          }),
      ),
    );
  }

  // Register the component (hidden tab — also reachable directly at
  // /api-providers) and the Models page slot that is the actual feature.
  window.__HERMES_PLUGINS__.register(PLUGIN, ApiProvidersCard);
  window.__HERMES_PLUGINS__.registerSlot(PLUGIN, "models:top", ApiProvidersCard);
})();
