Import("env")

from pathlib import Path


def make_uf2(source, target, env):
    framework = Path(env.PioPlatform().get_package_dir("framework-arduinoadafruitnrf52"))
    converter = framework / "tools" / "uf2conv" / "uf2conv.py"
    hex_path = Path(env.subst("$BUILD_DIR/${PROGNAME}.hex"))
    role = env.GetProjectOption("custom_role")
    output = hex_path.parent / f"heltec-t114-signal-{role}.uf2"
    command = f'"$PYTHONEXE" "{converter}" "{hex_path}" -c -f 0xADA52840 -o "{output}"'
    return env.Execute(command)


env.AddPostAction("$BUILD_DIR/${PROGNAME}.hex", make_uf2)
