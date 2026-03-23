import logging
import re


logger = logging.getLogger(__name__)


def _ha_bgp_oper(duthost, start=True):

    output = duthost.shell('show ip bgp summary')['stdout']
    logger.info(f"Current BGP Neighbors:\n, {output}")

    # Extract BGP neighbor IPs using regex
    # Assuming the neighbor IPs appear in a column, adjust this part based on actual output format
    neighbor_ips = re.findall(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b', output)

    # Shutdown each BGP neighbor
    for neighbor_ip in neighbor_ips:
        if start:
            bgp_command = f'config bgp shutdown neighbor {neighbor_ip}'
        else:
            bgp_command = f'config bgp start neighbor {neighbor_ip}'

        logger.info(f"BGP neighbor command: {bgp_command}")
        duthost.shell(bgp_command)


def ha_shutdown_bgp(duthost):

    return _ha_bgp_oper(duthost, False)


def ha_start_bgp(duthost):

    return _ha_bgp_oper(duthost, True)
