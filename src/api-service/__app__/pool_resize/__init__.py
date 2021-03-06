#!/usr/bin/env python
#
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import logging
import math
from typing import List

import azure.functions as func
from onefuzztypes.enums import NodeState, PoolState, ScalesetState
from onefuzztypes.models import AutoScaleConfig, TaskPool

from ..onefuzzlib.pools import Node, Pool, Scaleset
from ..onefuzzlib.tasks.main import Task


def scale_up(pool: Pool, scalesets: List[Scaleset], nodes_needed: int) -> None:
    logging.info("Scaling up")
    autoscale_config = pool.autoscale
    if not isinstance(autoscale_config, AutoScaleConfig):
        return

    for scaleset in scalesets:
        if scaleset.state == ScalesetState.running:

            max_size = min(scaleset.max_size(), autoscale_config.scaleset_size)
            logging.info(
                "Sacleset id: %s, Scaleset size: %d, max_size: %d"
                % (scaleset.scaleset_id, scaleset.size, max_size)
            )
            if scaleset.size < max_size:
                current_size = scaleset.size
                if nodes_needed <= max_size - current_size:
                    scaleset.size = current_size + nodes_needed
                    nodes_needed = 0
                else:
                    scaleset.size = max_size
                    nodes_needed = nodes_needed - (max_size - current_size)
                scaleset.state = ScalesetState.resize
                scaleset.save()

            else:
                continue

            if nodes_needed == 0:
                return

    for _ in range(
        math.ceil(
            nodes_needed
            / min(
                Scaleset.scaleset_max_size(autoscale_config.image),
                autoscale_config.scaleset_size,
            )
        )
    ):
        logging.info("Creating Scaleset for Pool %s" % (pool.name))
        max_nodes_scaleset = min(
            Scaleset.scaleset_max_size(autoscale_config.image),
            autoscale_config.scaleset_size,
            nodes_needed,
        )

        if not autoscale_config.region:
            raise Exception("Region is missing")

        scaleset = Scaleset.create(
            pool_name=pool.name,
            vm_sku=autoscale_config.vm_sku,
            image=autoscale_config.image,
            region=autoscale_config.region,
            size=max_nodes_scaleset,
            spot_instances=autoscale_config.spot_instances,
            tags={"pool": pool.name},
        )
        scaleset.save()
        nodes_needed -= max_nodes_scaleset


def scale_down(scalesets: List[Scaleset], nodes_to_remove: int) -> None:
    logging.info("Scaling down")
    for scaleset in scalesets:
        nodes = Node.search_states(
            scaleset_id=scaleset.scaleset_id, states=[NodeState.free]
        )
        if nodes and nodes_to_remove > 0:
            max_nodes_remove = min(len(nodes), nodes_to_remove)
            if max_nodes_remove >= scaleset.size and len(nodes) == scaleset.size:
                scaleset.state = ScalesetState.shutdown
                nodes_to_remove = nodes_to_remove - scaleset.size
                scaleset.save()
                for node in nodes:
                    node.set_shutdown()
                continue

            scaleset.size = scaleset.size - max_nodes_remove
            nodes_to_remove = nodes_to_remove - max_nodes_remove
            scaleset.state = ScalesetState.resize
            scaleset.save()


def get_vm_count(tasks: List[Task]) -> int:
    count = 0
    for task in tasks:
        task_pool = task.get_pool()
        if (
            not task_pool
            or not isinstance(task_pool, Pool)
            or not isinstance(task.config.pool, TaskPool)
        ):
            continue
        count += task.config.pool.count
    return count


def main(mytimer: func.TimerRequest) -> None:  # noqa: F841
    pools = Pool.search_states(states=PoolState.available())
    for pool in pools:
        logging.info("autoscale: %s" % (pool.autoscale))
        if not pool.autoscale:
            continue

        # get all the tasks (count not stopped) for the pool
        tasks = Task.get_tasks_by_pool_name(pool.name)
        logging.info("Pool: %s, #Tasks %d" % (pool.name, len(tasks)))

        num_of_tasks = get_vm_count(tasks)
        nodes_needed = max(num_of_tasks, pool.autoscale.min_size)
        if pool.autoscale.max_size:
            nodes_needed = min(nodes_needed, pool.autoscale.max_size)

        # do scaleset logic match with pool
        # get all the scalesets for the pool
        scalesets = Scaleset.search_by_pool(pool.name)
        pool_resize = False
        for scaleset in scalesets:
            if scaleset.state in ScalesetState.modifying():
                pool_resize = True
                break
            nodes_needed = nodes_needed - scaleset.size

        if pool_resize:
            continue

        logging.info("Pool: %s, #Nodes Needed: %d" % (pool.name, nodes_needed))
        if nodes_needed > 0:
            # resizing scaleset or creating new scaleset.
            scale_up(pool, scalesets, nodes_needed)
        elif nodes_needed < 0:
            scale_down(scalesets, abs(nodes_needed))
