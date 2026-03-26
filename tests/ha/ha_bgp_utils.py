import logging


logger = logging.getLogger(__name__)


def _ha_bgp_oper(duthost, start=True):

    cmd = 'show ip bgp summary'
    parse_result = duthost.show_and_parse(cmd)
    neighbor_ips = {entry['neighbor'] for entry in parse_result}
    # Shutdown or start each BGP neighbor
    for neighbor_ip in neighbor_ips:
        if start:
            bgp_command = f'config bgp start neighbor {neighbor_ip}'
        else:
            bgp_command = f'config bgp shutdown neighbor {neighbor_ip}'

        logger.info(f"BGP neighbor command: {bgp_command}")
        duthost.shell(bgp_command)


def ha_bgp_shutdown(duthost):

    return _ha_bgp_oper(duthost, False)


def ha_bgp_start(duthost):

    return _ha_bgp_oper(duthost, True)
