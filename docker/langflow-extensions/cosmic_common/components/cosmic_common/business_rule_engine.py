"""Business rule engine component (constitution §9).

Generic, reusable engine that evaluates a declarative rule set against a payload
and emits per-rule pass/fail. Scaffold only — pure logic at build phase. Rule
parse errors return ``code=AR_VALIDATION`` (§9); never raise out of the output
method.
"""

from lfx.custom import Component
from lfx.io import BoolInput, MultilineInput, Output
from lfx.schema import Message


class BusinessRuleEngineComponent(Component):
    name = "BusinessRuleEngineComponent"
    display_name = "Business Rule Engine"
    description = (
        "Evaluate a declarative rule set against a payload and return per-rule "
        "pass/fail. Call this to enforce AR business rules (e.g. 'no match above "
        "the auto ceiling without approval') before any financial action."
    )
    icon = "ListChecks"

    inputs = [
        MultilineInput(
            name="rules",
            display_name="Rules (JSON)",
            info='JSON array of rules, e.g. [{"rule_id":"R1","field":"amount","op":"<=","value":"1500.00"}].',
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="payload",
            display_name="Payload (JSON)",
            info="JSON object to evaluate the rules against.",
            required=True,
            tool_mode=True,
        ),
        BoolInput(
            name="strict",
            display_name="Strict",
            value=False,
            info="If true, any failing rule makes the overall result an error (AR_RULE_FAILED).",
        ),
    ]

    outputs = [
        Output(
            name="engine_output",
            display_name="Rule Results",
            method="evaluate",
        ),
    ]

    def evaluate(self) -> Message:
        # Placeholder — pure rule evaluation is build phase. Malformed rules
        # return AR_VALIDATION (§9). Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                '"data":{"results":[]}}'
            )
        )