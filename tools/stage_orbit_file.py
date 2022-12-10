#!/usr/bin/env python3

"""
===================
stage_orbit_file.py
===================

Script to query and download the appropriate Orbit Ephemeris file for the time
range covered by an input SLC SAFE archive.

"""

import argparse
import os
import re
import requests

from datetime import datetime, timedelta
from os.path import abspath

import lxml.etree as ET

from commons.logger import logger
from commons.logger import LogLevels

DEFAULT_QUERY_ENDPOINT = 'https://scihub.copernicus.eu/gnss/search'
"""Default URL endpoint for SciHub query REST service"""

DEFAULT_DOWNLOAD_ENDPOINT = 'https://scihub.copernicus.eu/gnss/odata/v1'
"""Default URL endpoint for SciHub download REST service"""

DEFAULT_USERNAME = 'gnssguest'
DEFAULT_PASSWORD = 'gnssguest'
"""Default username and password for a public account provided by SciHub"""


def get_parser():
    """Returns the command line parser for stage_orbit_file.py"""
    parser = argparse.ArgumentParser(
        description="Query and stage an Orbit Ephemeris file for use with an "
                    "SLC-based processing job. The appropriate Orbit file is "
                    "queried for based on the time range covered by an input SLC "
                    "swath. The swath time range is determined from the file name "
                    "of the desired SLC SAFE archive file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-o", "--output-directory", type=str, action='store',
                        default=abspath(os.curdir),
                        help="Specify the directory to store the output Orbit file. "
                             "Has no effect if --url-only is specified.")
    parser.add_argument("-u", "--username", type=str, action='store',
                        default=DEFAULT_USERNAME,
                        help="Specify a user name to use with the query/download "
                             "requests. Note that the default should be suitable "
                             "for use with this script.")
    parser.add_argument("-p", "--password", type=str, action='store',
                        default=DEFAULT_PASSWORD,
                        help="Specify a password to use with the query/download "
                             "requests. Note that the default should be suitable "
                             "for use with this script.")
    parser.add_argument("--url-only", action="store_true",
                        help="Only output the URL from where the resulting Orbit "
                             "file may be downloaded from.")
    parser.add_argument("--query-endpoint", type=str, action='store',
                        default=DEFAULT_QUERY_ENDPOINT, metavar='URL',
                        help="Specify the query service URL endpoint to which the "
                             "query itself will be appended.")
    parser.add_argument("--download-endpoint", type=str, action='store',
                        default=DEFAULT_DOWNLOAD_ENDPOINT, metavar='URL',
                        help="Specify the download service URL endpoint from which "
                             "the Orbit file will be obtained from. Has no effect when "
                             "--url-only is provided.")
    parser.add_argument("--log-level",
                        type=lambda log_level: LogLevels[log_level].value,
                        choices=LogLevels.list(),
                        default=LogLevels.INFO.value,
                        help="Specify a logging verbosity level.")
    parser.add_argument("input_safe_file", type=str, action='store',
                        help="Name of the input SLC SAFE archive to obtain the "
                             "corresponding Orbit file for. This may be the file "
                             "name only, or a full/relative path to the file.")

    return parser


def parse_orbit_time_range_from_safe(input_safe_file):
    """
    Parses the time range covered by the input SLC SAFE file, so it can be used
    with the query for a corresponding Orbit file. The mission ID (S1A or S1B)
    is also parsed, since this also becomes part of the query.

    Parameters
    ----------
    input_safe_file : str
        Name of the SAFE file to parse. May be just the file name or a path to
        the file.

    Returns
    -------
    mission_id : str
        The mission ID parsed from the SAFE file name, should always be one
        of S1A or S1B.
    safe_start_time : str
        The start time parsed from the SAFE file name in YYYYmmddTHHMMSS format.
    safe_stop_time : str
        The stop time parsed from the SAFE file name in YYYYmmddTHHMMSS format.

    Raises
    ------
    RuntimeError
        If the provided SAFE file name cannot be parsed according to the expected
        file name conventions.

    """
    # Remove any path and extension info from the provided file name
    safe_filename = os.path.splitext(os.path.basename(input_safe_file))[0]

    logger.debug(f'input_safe_file: {input_safe_file}')
    logger.debug(f'safe_filename: {safe_filename}')

    # Parse the SAFE file name with the following regex, derived from the
    # official naming conventions, which can be referenced here:
    # https://sentinels.copernicus.eu/web/sentinel/user-guides/sentinel-1-sar/naming-conventions
    safe_regex_pattern = (
        r"(?P<mission_id>S1A|S1B)_(?P<beam_mode>IW)_(?P<product_type>SLC)(?P<resolution>_)_"
        r"(?P<level>1)(?P<class>S)(?P<pol>SH|SV|DH|DV)_(?P<start_ts>\d{8}T\d{6})_"
        r"(?P<stop_ts>\d{8}T\d{6})_(?P<orbit_num>\d{6})_(?P<data_take_id>[0-9A-F]{6})_"
        r"(?P<product_id>[0-9A-F]{4})"
    )
    safe_regex = re.compile(safe_regex_pattern)
    match = safe_regex.match(safe_filename)

    if not match:
        raise RuntimeError(
            f'SAFE file name {safe_filename} does not conform to expected format'
        )

    # Extract the file name portions we care about
    mission_id = match.groupdict()['mission_id']
    safe_start_time = match.groupdict()['start_ts']
    safe_stop_time = match.groupdict()['stop_ts']

    logger.debug(f'mission_id: {mission_id}')
    logger.debug(f'safe_start_time: {safe_start_time}')
    logger.debug(f'safe_stop_time: {safe_stop_time}')

    return mission_id, safe_start_time, safe_stop_time


def construct_orbit_file_query(mission_id, safe_start_time, safe_stop_time):
    """
    Constructs the query used with the query endpoint URL to determine the
    available Orbit files for the given time range.

    The time range used with the query is widened by day on each side from the
    SAFE time range to ensure proper temporal coverage, as orbit files tend to
    span several days, whereas SAFE files only span several minutes.

    Parameters
    ----------
    mission_id : str
        The mission ID parsed from the SAFE file name, should always be one
        of S1A or S1B.
    safe_start_time : str
        The start time parsed from the SAFE file name in YYYYmmddTHHMMSS format.
    safe_stop_time : str
        The stop time parsed from the SAFE file name in YYYYmmddTHHMMSS format.

    Returns
    -------
    query : str
        The Orbit file query formatted as the payload the query service expects.

    """
    # Convert the start/stop time strings to datetime objects
    safe_start_date = datetime.strptime(safe_start_time, "%Y%m%dT%H%M%S")
    safe_stop_date = datetime.strptime(safe_stop_time, "%Y%m%dT%H%M%S")

    logger.debug(f'safe_start_date: {safe_start_date}')
    logger.debug(f'safe_stop_date: {safe_stop_date}')

    # Pad the start/stop times by a day on each side to ensure we can find
    # a corresponding Orbit file that encompasses the SAFE time range
    query_start_date = safe_start_date - timedelta(days=1)
    query_stop_date = safe_stop_date + timedelta(days=1)

    logger.debug(f'query_start_date: {query_start_date}')
    logger.debug(f'query_stop_date: {query_stop_date}')

    # Set up templates that use the domain specific syntax expected by the
    # query service
    time_range_template = "{start_date}T00:00:00.000Z TO {stop_date}T23:59:59.999Z"

    # TODO: this template will probably need to support AUX_RESORB as a
    #       producttype when we can't find anything for AUX_POEORB
    query_template = (
        "( beginPosition:[{start_range}] AND endPosition:[{stop_range}] ) AND "
        "( (platformname:Sentinel-1 AND filename:{mission_id}_* AND producttype:AUX_POEORB))"
    )

    # Format the query templates using the values we were provided
    query_start_range = time_range_template.format(
        start_date=query_start_date.strftime("%Y-%m-%d"),
        stop_date=safe_start_date.strftime("%Y-%m-%d")
    )

    query_stop_range = time_range_template.format(
        start_date=safe_stop_date.strftime("%Y-%m-%d"),
        stop_date=query_stop_date.strftime("%Y-%m-%d")
    )

    logger.debug(f'query_start_range: {query_start_range}')
    logger.debug(f'query_stop_range: {query_stop_range}')

    query = query_template.format(start_range=query_start_range,
                                  stop_range=query_stop_range,
                                  mission_id=mission_id)

    logger.debug(f'query: {query}')

    return query


def query_orbit_file_service(endpoint_url, query, username, password):
    """
    Submits a request to the Orbit file query REST service, and returns the
    XML-formatted response.

    Parameters
    ----------
    endpoint_url : str
        The URL for the query endpoint, to which the query is appended to as
        the payload.
    query : str
        The query for the Orbit files to find, filtered by a time range and mission
        ID corresponding to the provided SAFE SLC archive file.
    username : str
        The username to authenticate the request with.
    password : str
        The password to authenticate the request with.

    Returns
    -------
    xml_response : str
        The XML-formatted body of the request response.

    Raises
    ------
    RuntimeError
        If the request fails for any reason (HTTP return code other than 200).

    """
    # Query service expects the query payload assigned to a value named "q"
    payload = {'q': query}

    # Make the HTTP GET request on the endpoint URL with the provided credentials
    response = requests.get(endpoint_url, params=payload, auth=(username, password))

    logger.debug(f'response.url: {response.url}')
    logger.debug(f'response.status_code: {response.status_code}')

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        raise RuntimeError(
            f'Failed to query Orbit File Service at {endpoint_url}, '
            f'reason: {str(err)}'
        )

    # XML response should be within the text body of the response
    xml_response = response.text

    logger.debug(f'xml_response: {xml_response}')

    return xml_response


def parse_orbit_file_query_xml(query_xml):
    """
    Parses the XML-format query response text returned from a query request.

    Whether the query returned any hits is determined, and if so, the first
    result is used to obtain the product ID and Orbit file filename.
    This is the info we need to download the Orbit file from the download service.

    Parameters
    ----------
    query_xml : str
        The XML body containing the results of the Orbit file query.

    Raises
    ------
    RuntimeError
        If the required info cannot be parsed from XML, or if the results indicate
        no hits were returned for the query.

    """
    # Create an XML parser and create the element tree from the provided
    # text
    xml_parser = ET.XMLParser(ns_clean=True, encoding='utf-8')
    tree = ET.fromstring(query_xml.encode('utf-8'), parser=xml_parser)

    # Find the element that tells us how many hits we got for the query
    total_results_elems = tree.xpath('.//*[local-name()="totalResults"]')

    if not len(total_results_elems):
        raise RuntimeError(
            'Could not find a totalResults element within the provided XML'
        )

    # There should only ever by one totalResults element
    total_results_elem = total_results_elems[0]
    total_results = int(total_results_elem.text)

    logger.debug(f'total_results: {total_results}')

    if total_results < 1:
        raise RuntimeError('No results returned from parsed query results')

    # Parse the entry elements, there should be one for each hit we got from
    # the query
    entry_elems = tree.xpath('.//*[local-name()="entry"]')

    logger.debug(f'len(entry_elems): {len(entry_elems)}')

    if not len(entry_elems):
        raise RuntimeError('Could not find any "entry" tags within parsed query results')

    # TODO: for now, always take the first query hit, we'll probably need to
    #       figure out how to select the best result from multiple hits when
    #       such a case occurs
    entry_elem = entry_elems[0]

    # Get the request ID from the entry element, this is the primary piece of
    # info needed by the download service to acquire the Orbit file
    orbit_file_request_id = entry_elem.findtext('id', namespaces=tree.nsmap)

    logger.debug(f'orbit_file_request_id: {orbit_file_request_id}')

    # Get all the info elements, as one will tell us the offical file name to
    # assign to the downloaded Orbit file
    info_elems = entry_elem.findall('str', namespaces=tree.nsmap)

    logger.debug(f'len(info_elems): {len(info_elems)}')

    # Scan the info elements for the one that corresponds to the Orbit filename
    for info_elem in info_elems:
        if info_elem.get('name') == 'filename':
            orbit_file_name = info_elem.text
            break
    else:
        raise RuntimeError('Could not parse the Orbit file name from query results')

    logger.debug(f'orbit_file_name: {orbit_file_name}')

    # Return the two pieces of info we need to download the file
    return orbit_file_name, orbit_file_request_id


def download_orbit_file(request_url, output_directory, orbit_file_name, username, password):
    """
    Downloads an Orbit file using the provided request URL, which should contain
    the product ID for the file to download, as obtained from a query result.

    The output file is named according to the orbit_file_name parameter, and
    should correspond to the file name parsed from the query result. The output
    file is written to the directory indicated by output_directory.

    Parameters
    ----------
    request_url : str
        The full request URL, which includes the download endpoint, as well as
        a payload that contains the product ID for the Orbit file to be downloaded.
    output_directory : str
        The directory to store the downloaded Orbit file to.
    orbit_file_name : str
        The file name to assign to the Orbit file once downloaded to disk. This
        should correspond to the file name parsed from a query result.
    username : str
        The username to authenticate the request with.
    password : str
        The password to authenticate the request with.

    Returns
    -------
    output_orbit_file_path : str
        The full path to where the resulting Orbit file was downloaded to.

    Raises
    ------
    RuntimeError
        If the request fails for any reason (HTTP return code other than 200).

    """
    # Make the HTTP GET request to obtain the Orbit file contents
    response = requests.get(request_url, auth=(username, password))

    logger.debug(f'r.url: {response.url}')
    logger.debug(f'r.status_code: {response.status_code}')

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        raise RuntimeError(
            f'Failed to download Orbit file from {response.url}, reason: {str(err)}'
        )

    # Get the Orbit file contents from the response
    orbit_file_contents = response.text

    # Write the contents to disk
    output_orbit_file_path = os.path.join(output_directory, orbit_file_name)

    with open(output_orbit_file_path, 'w', encoding='utf-8') as outfile:
        outfile.write(orbit_file_contents)

    return output_orbit_file_path


def main(args):
    """
    Main script to execute Orbit file staging.

    Parameters
    ----------
    args: argparse.Namespace
        Arguments parsed from the command-line.

    """
    # Set the logging level
    if args.log_level:
        LogLevels.set_level(args.log_level)

    logger.info(f"Determining Orbit file for input SAFE file {args.input_safe_file}")

    # Parse the relevant info from the input SAFE filename
    (mission_id,
     safe_start_time,
     safe_stop_time) = parse_orbit_time_range_from_safe(args.input_safe_file)

    logger.info(f"Parsed time range {safe_start_time} - {safe_stop_time} from SAFE filename")

    # Construct the query based on the time range parsed from the input file
    query = construct_orbit_file_query(
        mission_id, safe_start_time, safe_stop_time
    )

    # Make the query to determine what Orbit files are available for the time
    # range
    logger.info(f"Querying for Orbit file(s) from endpoint {args.query_endpoint}")

    xml_response = query_orbit_file_service(
        args.query_endpoint, query, args.username, args.password
    )

    # Parse the XML response from the query service
    logger.info("Parsing XML response from Orbit query service")

    (orbit_file_name,
     orbit_file_request_id) = parse_orbit_file_query_xml(xml_response)

    # Construct the URL used to download the Orbit file
    request_url = os.path.join(
        args.download_endpoint, f"Products('{orbit_file_request_id}')/$value"
    )

    # If user request the URL only, print it to standard out
    if args.url_only:
        logger.info('URL-only requested')
        print(request_url)
    # Otherwise, download the Orbit file using the file name parsed from the
    # query result to the directory specified by the user
    else:
        logger.info(
            f"Downloading Orbit file {orbit_file_name} from service endpoint "
            f"{args.download_endpoint}"
        )
        output_orbit_file_path = download_orbit_file(
            request_url, args.output_directory, orbit_file_name, args.username,
            args.password
        )

        logger.info(f"Orbit file downloaded to {output_orbit_file_path}")


if __name__ == '__main__':
    parser = get_parser()
    args = parser.parse_args()
    main(args)
