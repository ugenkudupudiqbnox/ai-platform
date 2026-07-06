"""Configuration loader component (constitution §17).

Generic, reusable loader for non-secret run tunables held in LangFlow Global
Variables / per-flow config (§17: thresholds, dunning cadence, approval
ceilings, feature flags). Secret values are NEVER read here — credentials go
through Secret Global Variables + ``SecretStrInput`` (§16). Scaffold only — pure
env/Global-Variable lookup at build phase; never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class ConfigurationLoaderComponent(Component):
    name = "ConfigurationLoaderComponent"
    display_name = "Configuration Loader"
    description = (
        "Load non-secret run tunables (thresholds, dunning cadence, approval "
        "ceilings, feature flags) from LangFlow Global Variables / per-flow "
        "config (§17). Call this at run start instead of hard-coding tunables. "
        "Secrets are not read here — use a Secret Global Variable (§16)."
    )
    icon = "Cog"

    inputs = [
        DropdownInput(
            name="config_ref",
            display_name="Config",
            options=["ar.thresholds", "ar.dunning", "ar.approval", "ar.matching", "ar.tenants", "ar.feature_flags"],
            value="ar.thresholds",
            info="Named config set to load (resolved from Global Variables, §17).",
            tool_mode=True,
        ),
        MultilineInput(
            name="keys",
            display_name="Keys (optional filter)",
            info="One key per line to restrict the loaded config; omit to load all keys.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="tenant",
            display_name="Tenant",
            info="Optional tenant id to resolve tenant-scoped overrides.",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="config_output",
            display_name="Config",
            method="load",
        ),
    ]

    def load(self) -> Message:
        config_ref = self.config_ref or "ar.thresholds"
        # Placeholder — Global Variable / env lookup is build phase (§17). Emits
        # a config envelope. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"config_ref":"{config_ref}","values":{{}}}}}}'
            )
        )