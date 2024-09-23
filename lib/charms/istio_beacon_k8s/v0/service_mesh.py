# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""#Service Mesh Library.

The service mesh library is used to facilitate adding your charmed application to a service mesh.
The library leverages the `service_mesh` and `cross_model_mesh` interfaces.

##Consumer

To add service mesh support to your charm, instantiate a ServiceMeshConsumer object in the
`__init__` method of your charm:

```
from charms.istio_beacon_k8s.v0.service_mesh import Policy, ServiceMeshConsumer

...
try:
    self._mesh = ServiceMeshConsumer(
        self,
        policies=[
            Policy(
                relation="logging",
                endpoints=[
                    Endpoint(
                        hosts=[self._my_host_name],
                        ports=[HTTP_LISTEN_PORT],
                        methods=["GET"],
                        paths=["/foo"],
                    ),
                ],
                service=self._my_k8s_service(),
            ),
        ],
    )
except ops.TooManyRelatedAppsError as e:
    self.unit.status = BlockedStatus(e)
```

You will then receive the labels which you need to add your product to the mesh:
```
def _on_mesh_relation_changed(self, event):
    self._apply_labels to pods(self._mesh.labels())

##Provider

To provide a service mesh, instantiate the ServiceMeshProvider object in the __init__ method
of your charm:
```
from charms.istio_beacon_k8s.v0.service_mesh import Policy, ServiceMeshProvider

...
self._mesh = ServiceMeshProvider(
    charm = self,
    labels = self._get_mesh_labels(),
    mesh_relation_name = "service-mesh",
)
```

The labels argument should be a dict containing the Kubernetes labels which the client charm needs
to apply to join the mesh.

You can then use the relation data to build your authorization policies:
```
self._build_authorization_policies(self._mesh.mesh_info())
```
"""

import enum
import json
import logging
from typing import Dict, List, Optional

import pydantic
from ops import CharmBase, Object, Relation

LIBID = "3f40cb7e3569454a92ac2541c5ca0a0c"  # Never change this
LIBAPI = 0
LIBPATCH = 1

PYDEPS = ["pydantic"]

logger = logging.getLogger(__name__)


class Method(str, enum.Enum):
    """HTTP method."""

    connect = "CONNECT"
    delete = "DELETE"
    get = "GET"
    head = "HEAD"
    options = "OPTIONS"
    patch = "PATCH"
    post = "POST"
    put = "PUT"
    trace = "TRACE"


class Endpoint(pydantic.BaseModel):
    """Data type for a policy endpoint."""

    hosts: List[str]
    ports: List[int]
    methods: List[Method]
    paths: List[str]


class Policy(pydantic.BaseModel):
    """Data type for defining a policy for your charm."""

    relation: str
    endpoints: List[Endpoint]
    service: Optional[str]


class MeshPolicy(pydantic.BaseModel):
    """Data type for storage service mesh policy information."""

    source_app_name: str
    source_namespace: str
    target_app_name: str
    target_namespace: str
    target_service: Optional[str]
    endpoints: List[Endpoint]


class CMRData(pydantic.BaseModel):
    """Data type containing the info required for cross-model relations."""

    app_name: str
    juju_model_name: str


class ServiceMeshConsumer(Object):
    """Class used for joining a service mesh."""

    def __init__(
        self,
        charm: CharmBase,
        mesh_relation_name: str = "service-mesh",
        cross_model_mesh_requires_name: str = "require-cmr-mesh",
        cross_model_mesh_provides_name: str = "provide-cmr-mesh",
        policies: Optional[List[Policy]] = None,
    ):
        """Class used for joining a service mesh.

        Args:
            charm: The charm instantiating this object.
            mesh_relation_name: The relation name as defined in metadata.yaml or charmcraft.yaml
                for the relation which uses the service_mesh interface.
            cross_model_mesh_requires_name: The relation name as defined in metadata.yaml or
                charmcraft.yaml for the relation which requires the cross_model_mesh interface.
            cross_model_mesh_provides_name: The relation name as defined in metadata.yaml or
                charmcraft.yaml for the relation which provides the cross_model_mesh interface.
            policies: List of access policies this charm supports.
        """
        super().__init__(charm, mesh_relation_name)
        self._charm = charm
        self._relation = self._charm.model.get_relation(mesh_relation_name)
        self._cmr_relations = self._charm.model.relations[cross_model_mesh_provides_name]
        self._policies = policies or []
        self.framework.observe(
            self._charm.on[mesh_relation_name].relation_created, self._relations_changed
        )
        self.framework.observe(
            self._charm.on[cross_model_mesh_requires_name].relation_created, self._send_cmr_data
        )
        self.framework.observe(self._charm.on.upgrade_charm, self._relations_changed)
        for policy in self._policies:
            self.framework.observe(
                self._charm.on[policy.relation].relation_created, self._relations_changed
            )
            self.framework.observe(
                self._charm.on[policy.relation].relation_broken, self._relations_changed
            )

    def _send_cmr_data(self, event):
        """Send app and model information for CMR."""
        data = CMRData(
            app_name=self._charm.app.name, juju_model_name=self._charm.model.name
        ).model_dump()
        event.relation.data[self._charm.app]["cmr_data"] = json.dumps(data)

    def _relations_changed(self, _event):
        self.update_service_mesh()

    def update_service_mesh(self):
        """Update the service mesh.

        Gathers information from all relations of the charm and updates the mesh appropriately to
        allow communication.
        """
        if self._relation is None:
            return
        logger.debug("Updating service mesh policies.")
        mesh_policies = []
        for policy in self._policies:
            for relation in self._charm.model.relations[policy.relation]:
                if cmr_relation := self._check_cmr(relation):
                    logger.debug(f"Found cross model relation: {relation.name}. Creating policy.")
                    cmr_data = CMRData.parse_obj(
                        json.loads(cmr_relation.data[cmr_relation.app]["cmr_data"])
                    )
                    mesh_policies.append(
                        MeshPolicy(
                            source_app_name=cmr_data.app_name,
                            source_namespace=cmr_data.juju_model_name,
                            target_app_name=self._charm.app.name,
                            target_namespace=self._my_namespace(),
                            target_service=policy.service,
                            endpoints=policy.endpoints,
                        ).model_dump()
                    )
                else:
                    logger.debug(f"Found relation: {relation.name}. Creating policy.")
                    mesh_policies.append(
                        MeshPolicy(
                            source_app_name=relation.app.name,
                            source_namespace=self._my_namespace(),
                            target_app_name=self._charm.app.name,
                            target_namespace=self._my_namespace(),
                            target_service=policy.service,
                            endpoints=policy.endpoints,
                        ).model_dump()
                    )
        self._relation.data[self._charm.app]["policies"] = json.dumps(mesh_policies)

    def _check_cmr(self, mesh_relation: Relation) -> Optional[Relation]:
        for cmr_rel in self._cmr_relations:
            if cmr_rel.app.name == mesh_relation.app.name:
                if "cmr_data" not in cmr_rel.data[cmr_rel.app]:
                    # Data has not yet been provided
                    continue
                return cmr_rel
        return None

    def _my_namespace(self):
        """Return the namespace of the running charm."""
        # This method currently assumes the namespace is the same as the model name. We
        # should consider if there is a better way to do this.
        return self._charm.model.name

    def labels(self):
        """Labels required for a pod to join the mesh."""
        if self._relation is None:
            return {}
        return json.loads(self._relation.data[self._relation.app]["labels"])


class ServiceMeshProvider(Object):
    """Provide a service mesh to applications."""

    def __init__(
        self, charm: CharmBase, labels: Dict[str, str], mesh_relation_name: str = "service-mesh"
    ):
        """Class used to provide information needed to join the service mesh.

        Args:
            charm: The charm instantiating this object.
            mesh_relation_name: The relation name as defined in metadata.yaml or charmcraft.yaml
                for the relation which uses the service_mesh interface.
            labels: The labels which related applications need to apply to use the mesh.
        """
        super().__init__(charm, mesh_relation_name)
        self._charm = charm
        self._relation_name = mesh_relation_name
        self._labels = labels
        self.framework.observe(
            self._charm.on[mesh_relation_name].relation_created, self._relation_created
        )

    def _relation_created(self, _event):
        self.update_relations()

    def update_relations(self):
        """Update all relations with the labels needed to use the mesh."""
        rel_data = json.dumps(self._labels)
        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.app]["labels"] = rel_data

    def mesh_info(self):
        """Return the relation data used to define authorization policies on the mesh."""
        mesh_info = []
        for relation in self._charm.model.relations[self._relation_name]:
            rel_data = json.loads(relation.data[relation.app]["mesh_data"])
            rel_data["policies"] = [Policy.parse_obj(p) for p in rel_data["policies"]]
            mesh_info.append(rel_data)
        return mesh_info
