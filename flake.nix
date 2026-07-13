{
  description = "Dev shell for reachy_mini_conversation_app (uv-based)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = pkgs.lib;
        python = pkgs.python312;

        # GStreamer stack for the reachy-mini daemon's media server
        # (imported via PyGObject: gi.require_version("Gst", "1.0")).
        gstPackages = with pkgs.gst_all_1; [
          gstreamer
          gst-plugins-base
          gst-plugins-good
          gst-plugins-bad
          gst-plugins-rs # webrtcsink, needed by the daemon's media server
        ];

        # Native libraries that manylinux wheels (av, aiortc, opencv,
        # sounddevice, onnxruntime, torch, ...) dlopen at runtime.
        libs = with pkgs; [
          stdenv.cc.cc.lib # libstdc++
          zlib
          glib
          libGL
          alsa-lib
          portaudio
          libv4l
        ] ++ gstPackages;

        # Build-time deps for sdists (pycairo/pygobject via reachy-mini).
        buildDeps = with pkgs; [ cairo glib gobject-introspection libffi ];

        envVars = {
          # Use the Nix-provided interpreter; uv's downloaded standalone
          # Python does not run on NixOS.
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = python.interpreter;
          LD_LIBRARY_PATH = lib.makeLibraryPath libs;
          # Let PyGObject find the Gst/GLib typelibs and GStreamer plugins.
          # getLib: typelibs/plugins live in the lib/out output, not bin.
          GI_TYPELIB_PATH = lib.makeSearchPath "lib/girepository-1.0"
            (map lib.getLib (gstPackages ++ [ pkgs.glib pkgs.gobject-introspection ]));
          GST_PLUGIN_SYSTEM_PATH_1_0 =
            lib.makeSearchPath "lib/gstreamer-1.0" (map lib.getLib gstPackages);
        };

        exportEnv = lib.concatStrings
          (lib.mapAttrsToList (n: v: "export ${n}=${lib.escapeShellArg v}\n") envVars);

        devRunner = pkgs.writeShellApplication {
          name = "reachy-dev";
          runtimeInputs = [ pkgs.uv python pkgs.curl pkgs.pkg-config ];
          text = ''
            ${exportEnv}
            # So uv can build pycairo/pygobject sdists outside the dev shell.
            export PKG_CONFIG_PATH=${lib.makeSearchPath "lib/pkgconfig" (map lib.getDev buildDeps)}

            # mediapipe_vision: the daemon spawns the app with no CLI args, so the
            # default head tracker (mediapipe) must be importable.
            uv sync --extra mediapipe_vision

            # The daemon scrubs GI_TYPELIB_PATH / GST_PLUGIN_SYSTEM_PATH_1_0
            # from the env of app subprocesses and expects the venv to restore
            # them at interpreter startup (a la the desktop app's
            # gstreamer_bundle.pth). Provide that .pth pointing at Nix paths.
            cat > .venv/lib/python${python.pythonVersion}/site-packages/zz-nix-gst-env.pth <<EOF
            import os; os.environ.setdefault("GI_TYPELIB_PATH", "${envVars.GI_TYPELIB_PATH}"); os.environ.setdefault("GST_PLUGIN_SYSTEM_PATH_1_0", "${envVars.GST_PLUGIN_SYSTEM_PATH_1_0}")
            EOF

            echo "Starting reachy-mini-daemon in mockup simulation mode..."
            uv run reachy-mini-daemon --mockup-sim &
            daemon_pid=$!
            cleanup() {
              curl -sf -X POST http://localhost:8000/api/apps/stop-current-app -o /dev/null || true
              kill "$daemon_pid" 2>/dev/null || true
            }
            trap cleanup EXIT

            echo "Waiting for daemon on http://localhost:8000 ..."
            until curl -sf -o /dev/null http://localhost:8000/; do
              if ! kill -0 "$daemon_pid" 2>/dev/null; then
                echo "Daemon exited before becoming ready." >&2
                exit 1
              fi
              sleep 0.5
            done

            echo "Daemon ready. Starting conversation app via the app runtime..."
            curl -sf -X POST http://localhost:8000/api/apps/stop-current-app -o /dev/null || true
            curl -sf -X POST http://localhost:8000/api/apps/start-app/reachy_mini_conversation_app

            echo
            echo "Dashboard: http://localhost:8000"
            echo "App UI:    http://localhost:7860"
            wait "$daemon_pid"
          '';
        };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
          ];

          # Build-time deps for sdists (pycairo/pygobject via reachy-mini).
          nativeBuildInputs = [ pkgs.pkg-config ];
          buildInputs = buildDeps;

          env = envVars;
        };

        apps.dev = {
          type = "app";
          program = lib.getExe devRunner;
        };
      });
}
