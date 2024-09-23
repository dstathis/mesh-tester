#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import ops
from charms.istio_beacon_k8s.v0.service_mesh import Endpoint, Policy, ServiceMeshConsumer

logger = logging.getLogger(__name__)


class MeshTesterCharm(ops.CharmBase):

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        policies = [
            Policy(
                relation="foo-p",
                endpoints=[Endpoint(hosts=[], ports=[80], methods=[], paths=[])],
                service=None,
            )
        ]
        self.mesh = ServiceMeshConsumer(charm=self, policies=policies)
        self.framework.observe(self.on.start, self.on_start)

    def on_start(self, _event):
        self.model.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(MeshTesterCharm)  # type: ignore
