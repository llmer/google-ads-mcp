# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test helpers for tools that mutate Google Ads entities.

Tools run against a real GoogleAdsClient, so operations are built from real
protos and field or enum mistakes fail the tests; only the RPCs are faked.
"""

import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v25.errors.types.policy_finding_error import (
    PolicyFindingErrorEnum,
)
from google.ads.googleads.v25.errors.types.policy_violation_error import (
    PolicyViolationErrorEnum,
)
from google.oauth2.credentials import Credentials


def make_client() -> GoogleAdsClient:
    return GoogleAdsClient(
        credentials=Credentials(token="fake"),
        developer_token="fake",
        use_proto_plus=True,
    )


def policy_exception(client) -> GoogleAdsException:
    """A failure as the API reports policy problems on an ad and a keyword."""
    failure = client.get_type("GoogleAdsFailure")

    finding = client.get_type("GoogleAdsError")
    finding.message = "The resource has been disapproved."
    finding.error_code.policy_finding_error = (
        PolicyFindingErrorEnum.PolicyFindingError.POLICY_FINDING
    )
    entry = client.get_type("PolicyTopicEntry")
    entry.topic = "DESTINATION_MISMATCH"
    entry.type_ = client.enums.PolicyTopicEntryTypeEnum.PROHIBITED
    evidence = client.get_type("PolicyTopicEvidence")
    evidence.text_list.texts.append("example.org")
    entry.evidences.append(evidence)
    finding.details.policy_finding_details.policy_topic_entries.append(entry)
    failure.errors.append(finding)

    violation = client.get_type("GoogleAdsError")
    violation.message = "A policy was violated."
    violation.error_code.policy_violation_error = (
        PolicyViolationErrorEnum.PolicyViolationError.POLICY_ERROR
    )
    details = violation.details.policy_violation_details
    details.external_policy_name = "Healthcare and medicines"
    details.key.policy_name = "PHARMACY"
    details.key.violating_text = "cheap pills"
    details.is_exemptible = True
    failure.errors.append(violation)
    return GoogleAdsException(None, None, failure, "req-2")


class MutateToolTestCase(unittest.TestCase):
    """Patches the client, GAQL searches and GoogleAdsService.Mutate.

    Attributes:
        client: The real GoogleAdsClient the tools use.
        service: The fake service returned for every service name.
        search_results: Rows returned by successive mutations.search calls.
    """

    def setUp(self):
        self.client = make_client()
        self.service = MagicMock()
        self.service.mutate.side_effect = self._fake_mutate
        self.search_results: List[List[Dict[str, Any]]] = []
        self.queries: List[str] = []

        patches = [
            patch(
                "ads_mcp.utils.get_googleads_client", return_value=self.client
            ),
            patch("ads_mcp.mutations.get_service", return_value=self.service),
            patch("ads_mcp.mutations.search", side_effect=self._fake_search),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_search(self, client, customer_id, query):
        self.queries.append(query)
        return self.search_results.pop(0) if self.search_results else []

    def _fake_mutate(self, request):
        """Returns a response with a result per operation, as the API does.

        Mirrors the generated client: validate_only is only accepted on the
        request message, not as a keyword argument.
        """
        customer_id = request.customer_id
        mutate_operations = request.mutate_operations
        response = self.client.get_type("MutateGoogleAdsResponse")
        if request.validate_only:
            return response
        for index, operation in enumerate(mutate_operations):
            op_field = operation._pb.WhichOneof("operation")
            resource = op_field.removesuffix("_operation")
            result = self.client.get_type("MutateOperationResponse")
            collection = "".join(w.capitalize() for w in resource.split("_"))
            collection = collection[0].lower() + collection[1:] + "s"
            getattr(result, f"{resource}_result").resource_name = (
                f"customers/{customer_id}/{collection}/{1000 + index}"
            )
            response.mutate_operation_responses.append(result)
        return response

    def mutate_calls(self):
        """Returns the operations of each Mutate call as proto-plus messages."""
        return [
            list(call.kwargs["request"].mutate_operations)
            for call in self.service.mutate.call_args_list
        ]

    @staticmethod
    def created(operations, operation_field):
        """Returns the created messages of the given operation type."""
        return [
            getattr(op, operation_field).create
            for op in operations
            if op._pb.WhichOneof("operation") == operation_field
            and getattr(op, operation_field)._pb.WhichOneof("operation")
            == "create"
        ]
