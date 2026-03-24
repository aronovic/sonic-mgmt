import pytest
import logging
from tests.common.helpers.assertions import pytest_assert
from tests.ha.ha_utils import set_dead_dash_ha_scope, verify_ha_state, activate_primary_dash_ha, \
    activate_secondary_dash_ha, teardown_dash_ha_from_json
from tests.ha.conftest import setup_dash_ha_utility, activate_dash_ha_utility, setup_gnmi_server


pytestmark = [
    pytest.mark.topology("t1-smartswitch-ha")
]

logger = logging.getLogger(__name__)


"""
The assumption for the test is that duthosts has primary and secondary in this order
"""


def test_ha_states(duthosts, localhost, ptfhost, setup_ha_config, setup_dash_ha_from_json, activate_dash_ha_from_json):
    """
    Set primary to dead and verify that it gets to dead state and secondary to standalone
    After that, bring back the primary and verify it was done
    """
    set_dead_dash_ha_scope(localhost, duthosts[0], ptfhost, "vdpu0_0:haset0_0")

    pytest_assert(verify_ha_state(duthosts[0], "vdpu0_0:haset0_0", "dead"),
                  "Primary HA state is not dead")

    pytest_assert(verify_ha_state(duthosts[1], "vdpu1_0:haset0_0", "standalone"),
                  "Secondary HA state is not standalone")

    pytest_assert(activate_primary_dash_ha(localhost, duthosts[0], ptfhost, "vdpu0_0:haset0_0", "activate_role"),
                  "Failed to activate HA on primary")

    logger.info(f"{duthosts[0].hostname} state transitions OK")
    """
    Set secondary to dead and verify that it gets to dead state and primary to standalone
    After that, bring back secondary and verify it was done
    """
    set_dead_dash_ha_scope(localhost, duthosts[1], ptfhost, "vdpu1_0:haset0_0")

    pytest_assert(verify_ha_state(duthosts[1], "vdpu1_0:haset0_0", "dead"),
                  "Secondary HA state is not dead")

    pytest_assert(verify_ha_state(duthosts[0], "vdpu0_0:haset0_0", "standalone"),
                  "Primary HA state is not standalone")

    pytest_assert(activate_secondary_dash_ha(localhost, duthosts[1], ptfhost, "vdpu1_0:haset0_0", "activate_role"),
                  "Failed to activate HA on secondary")

    logger.info(f"{duthosts[1].hostname} state transitions OK")


def test_ha_cleanup_and_reconfigure(request, duthosts, localhost, ptfhost, setup_ha_config,
                                    setup_dash_ha_from_json, activate_dash_ha_from_json):
    """
    Cleanup HA configuration after activation
    """
    set_dead_dash_ha_scope(localhost, duthosts[0], ptfhost, "vdpu0_0:haset0_0")
    pytest_assert(verify_ha_state(duthosts[0], "vdpu0_0:haset0_0", "dead"),
                  "Primary HA state is not dead")

    set_dead_dash_ha_scope(localhost, duthosts[1], ptfhost, "vdpu1_0:haset0_0")
    pytest_assert(verify_ha_state(duthosts[1], "vdpu1_0:haset0_0", "dead"),
                  "Secondary HA state is not dead")
    logger.info("Teardown the HA configuration")
    teardown_dash_ha_from_json(duthosts=duthosts, localhost=localhost, ptfhost=ptfhost)

    # Reactivate HA to verify that configuration was removed properly
    logger.info("Reactivate the HA configuration")
    setup_dash_ha_utility(duthosts, localhost, ptfhost, setup_gnmi_server)
    activate_dash_ha_utility(duthosts, localhost, ptfhost, setup_gnmi_server)
