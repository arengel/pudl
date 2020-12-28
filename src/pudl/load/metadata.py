"""
Routines for generating PUDL tabular data package and resource metadata.

This module enables the generation and use of the metadata for tabular data
packages. It also saves and validates the datapackage once the
metadata is compiled. In general the routines in this module can only be used
**after** the referenced CSV's have been generated by the top level PUDL ETL
module, and written out to the datapackage data directory by the
`pudl.load.csv` module.

The metadata comes from three basic sources: the datapkg_settings that are read
in from the YAML file specifying the datapackage or bundle of datapackages to
be generated, the CSV files themselves (their names, sizes, and hash values)
and the stored metadata template which ultimately determines the structure of
the relational database that these output tabular data packages represent, and
encodes field specific table schemas. See the "megadata" which is stored in
`src/pudl/package_data/meta/datapkg/datapackage.json`.

For unpartitioned tables which are contained in a single tabular data resource
this is a relatively straightforward process. However, larger tables that have
been partitioned into smaller tabular data resources that are part of a
resource group (e.g. EPA CEMS) have additional complexities. We have tried to
say "resource" when referring to an individual output CSV that has its own
metadata entry, and "table" when referring to whole tables which typically
contain only a single resource, but may be composed of hundreds or even
thousands of individual resources.

See https://frictionlessdata.io for more details on the tabular data package
standards.

In addition, we have included PUDL specific metadata fields that document the
ETL parameters which were used to process the data, temporal and spatial
coverage for each resource, Zenodo DOIs if appropriate, UUIDs to identify the
individual data packages as well as co-generated bundles of data packages that
can be used together to instantiate a single database, etc.

"""
import datetime
import hashlib
import importlib
import json
import logging
import pathlib
import re
import uuid

import datapackage
import goodtables_pandas as goodtables
import pkg_resources

import pudl
from pudl import constants as pc

logger = logging.getLogger(__name__)

##############################################################################
# CREATING PACKAGES AND METADATA
##############################################################################


def hash_csv(csv_path):
    """Calculates a SHA-256 hash of the CSV file for data integrity checking.

    Args:
        csv_path (path-like) : Path the CSV file to hash.

    Returns:
        str: the hexdigest of the hash, with a 'sha256:' prefix.

    """
    # how big of a bit should I take?
    blocksize = 65536
    # sha256 is the fastest relatively secure hashing algorith.
    hasher = hashlib.sha256()
    # opening the file and eat it for lunch
    with open(csv_path, 'rb') as afile:
        buf = afile.read(blocksize)
        while len(buf) > 0:
            hasher.update(buf)
            buf = afile.read(blocksize)

    # returns the hash
    return f"sha256:{hasher.hexdigest()}"


def compile_partitions(datapkg_settings):
    """
    Given a datapackage settings dictionary, extract dataset partitions.

    Iterates through all the datasets enumerated in the datapackage settings,
    and compiles a dictionary indicating which datasets should be partitioned
    and on what basis when they are output as tabular data resources. Currently
    this only applies to the epacems dataset. Datapackage settings must be
    validated because currently we inject EPA CEMS partitioning variables
    (epacems_years, epacems_states) during the validation process.

    Args:
        datapkg_settings (dict): a dictionary containing validated datapackage
            settings, mostly read in from a PUDL ETL settings file.

    Returns:
        dict: Uses table name (e.g. hourly_emissions_epacems)  as keys, and
        lists of partition variables (e.g. ["epacems_years", "epacems_states"])
        as the values. If no datasets within the datapackage are being
        partitioned, this is an empty dictionary.

    """
    partitions = {}
    for dataset in datapkg_settings['datasets']:
        for dataset_name in dataset:
            try:
                partitions.update(dataset[dataset_name]['partition'])
            except KeyError:
                pass
    return partitions


def get_unpartitioned_tables(resources, datapkg_settings):
    """
    Generate a list of database table names from a list of data resources.

    In the case of EPA CEMS and potentially other large datasets, we are
    partitioning a single table into many tabular data resources that are
    part of a resource group. However in some contexts we want to refer to the
    list of corresponding databse tables, rather than the list of resources.

    The partition key in the datapackage settings is the name of the table
    without the partition elements, and so in the case of partitioned tables
    we use that key as the name of the table. Otherwise we just use the name
    of the resource.

    Args:
        resources (iterable): A list of tabular data resource names. They must
            be expected to appear in the datapackage specified by
            datapkg_settings.
        datapkg_settings (dict): a dictionary containing validated datapackage
            settings, mostly read in from a PUDL ETL settings file.

    Returns:
        list: The names of the database tables corresponding to the tabular
            datapackage resource names that were passed in.

    """
    partitions = compile_partitions(datapkg_settings)
    tables_unpartitioned = set()
    if not partitions:
        tables_unpartitioned = resources
    else:
        for resource in resources:
            for table in partitions.keys():
                if table in resource:
                    tables_unpartitioned.add(table)
                else:
                    tables_unpartitioned.add(resource)

    return tables_unpartitioned


def data_sources_from_tables(table_names):
    """
    Look up data sources used by the given list of PUDL database tables.

    Args:
        tables_names (iterable): a list of names of 'seed' tables, whose
            dependencies we are seeking to find.

    Returns:
        set: The set of data sources for the list of PUDL table names.

    """
    all_tables = get_dependent_tables_from_list(table_names)
    table_sources = set()
    # All tables get PUDL:
    table_sources.add('pudl')
    for t in all_tables:
        for src in pc.data_sources:
            if re.match(f".*_{src}$", t):
                table_sources.add(src)

    return table_sources


def get_datapkg_fks(datapkg_json):
    """
    Get a dictionary of foreign key relationships from datapackage metadata.

    Args:
        datapkg_json (path-like): Path to the datapackage.json
            containing the schema from which the foreign key relationships
            will be read.

    Returns:
        dict: table names (keys) with lists of table names (values) which the
            key table has forgien key relationships with.

    """
    with open(datapkg_json) as md:
        metadata = json.load(md)

    fk_relash = {}
    for tbl in metadata['resources']:
        fk_relash[tbl['name']] = []
        if 'foreignKeys' in tbl['schema']:
            fk_tables = []
            for fk in tbl['schema']['foreignKeys']:
                fk_tables.append(fk['reference']['resource'])
            fk_relash[tbl['name']] = fk_tables
    return fk_relash


def get_dependent_tables(table_name, fk_relash):
    """
    For a given table, get the list of all the other tables it depends on.

    Args:
        table_name (str): The table whose dependencies we are looking for.
        fk_relash (dict): table names (keys) with lists of table names (values)
            which the key table has forgien key relationships with.

    Returns:
        set: the set of all the tables the specified table depends upon.

    """
    # Add the initial table
    dependent_tables = set()
    dependent_tables.add(table_name)

    # Get the list of tables this table depends on:
    new_table_names = set()
    new_table_names.update(fk_relash[table_name])

    # Recursively call this function on the tables our initial
    # table depends on:
    for table_name in new_table_names:
        logger.debug(f"Finding dependent tables for {table_name}")
        dependent_tables.add(table_name)
        for t in get_dependent_tables(table_name, fk_relash):
            dependent_tables.add(t)

    return dependent_tables


def get_dependent_tables_from_list(table_names):
    """
    Given a list of tables, find all the other tables they depend on.

    Iterate over a list of input tables, adding them and all of their dependent
    tables to a set, and return that set. Useful for determining which tables
    need to be exported together to yield a self-contained subset of the PUDL
    database.

    Args:
        table_names (iterable): a list of names of 'seed' tables, whose
            dependencies we are seeking to find.

    Returns:
        set: All tables with which any of the input tables have ForeignKey
        relations.

    """
    with importlib.resources.path('pudl.package_data.meta.datapkg',
                                  'datapackage.json') as datapkg_json:
        fk_relash = get_datapkg_fks(datapkg_json)

        all_the_tables = set()
        for t in table_names:
            for x in get_dependent_tables(t, fk_relash):
                all_the_tables.add(x)

    return all_the_tables


def pull_resource_from_megadata(resource_name):
    """
    Read metadata for a given data resource from the stored PUDL megadata.

    Args:
        resource_name (str): the name of the tabular data resource whose JSON
            descriptor we are reading.

    Returns:
        dict: A Python dictionary containing the resource descriptor portion of
        a data package descriptor, not expected to be valid or complete.

    Raises:
        ValueError: If table_name is not found exactly one time in the PUDL
            metadata library.

    """
    with importlib.resources.open_text('pudl.package_data.meta.datapkg',
                                       'datapackage.json') as datapkg_json:
        metadata_mega = json.load(datapkg_json)
    # bc we partition the CEMS output, the CEMS table name includes the state,
    # year or other partition.. therefor we need to assume for the sake of
    # grabing metadata that any table name that includes the table name is cems
    if "hourly_emissions_epacems" in resource_name:
        table_name_mega = "hourly_emissions_epacems"
    else:
        table_name_mega = resource_name
    table_resource = [
        x for x in metadata_mega['resources'] if x['name'] == table_name_mega
    ]

    if len(table_resource) == 0:
        raise ValueError(f"{resource_name} not found in stored metadata.")
    if len(table_resource) > 1:
        raise ValueError(f"{resource_name} found multiple times in metadata.")
    table_resource = table_resource[0]
    # rename the resource name to the og table name
    # this is important for the partitioned tables in particular
    table_resource['name'] = resource_name
    return table_resource


def spatial_coverage(resource_name):
    """
    Extract spatial coverage (country and state) for a given source.

    Args:
        resource_name (str): The name of the (potentially partitioned) resource
            for which we are enumerating the spatial coverage. Currently this
            is the only place we are able to access the partitioned spatial
            coverage after the ETL process has completed.

    Returns:
        dict: A dictionary containing country and potentially state level
        spatial coverage elements. Country keys are "country" for the full name
        of country, "iso_3166-1_alpha-2" for the 2-letter ISO code, and
        "iso_3166-1_alpha-3" for the 3-letter ISO code. State level elements
        are "state" (a two letter ISO code for sub-national jurisdiction) and
        "iso_3166-2" for the combined country-state code conforming to that
        standard.

    """
    coverage = {
        "country": "United States of America",
        # More generally... ISO 3166-1 2-letter country code:
        "iso_3166-1_alpha-2": "US",
        # More generally... ISO 3166-1 3-letter country code:
        "iso_3166-1_alpha-3": "USA",
    }
    if "hourly_emissions_epacems" in resource_name:
        us_state = resource_name.split("_")[4].upper()
        coverage["state"] = us_state
        # ISO3166-2:US code for the relevant state or outlying area:
        coverage["iso_3166-2"] = f"US-{us_state}"
    return coverage


def temporal_coverage(resource_name, datapkg_settings):
    """Extract start and end dates from ETL parameters for a given source.

    Args:
        resource_name (str): The name of the (potentially partitioned) resource
            for which we are enumerating the spatial coverage. Currently this
            is the only place we are able to access the partitioned spatial
            coverage after the ETL process has completed.
        datapkg_settings (dict): Python dictionary represeting the ETL
            parameters read in from the settings file, pertaining to the
            tabular datapackage this resource is part of.

    Returns:
        dict: A dictionary of two items, keys "start_date" and "end_date" with
        values in ISO 8601 YYYY-MM-DD format, indicating the extent of the
        time series data contained within the resource. If the resource does
        not contain time series data, the dates are null.

    """
    start_date = None
    end_date = None
    if "hourly_emissions_epacems" in resource_name:
        year = resource_name.split("_")[3]
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"
    else:
        source_years = f"{resource_name.split('_')[-1]}_years"
        for dataset in datapkg_settings["datasets"]:
            etl_params = list(dataset.values())[0]
            try:
                start_date = f"{min(etl_params[source_years])}-01-01"
                end_date = f"{max(etl_params[source_years])}-12-31"
                break
            except KeyError:
                continue

    return {"start_date": start_date, "end_date": end_date}


def get_tabular_data_resource(resource_name, datapkg_dir,
                              datapkg_settings, partitions=False):
    """
    Create a Tabular Data Resource descriptor for a PUDL table.

    Based on the information in the database, and some additional metadata this
    function will generate a valid Tabular Data Resource descriptor, according
    to the Frictionless Data specification, which can be found here:
    https://frictionlessdata.io/specs/tabular-data-resource/

    Args:
        resource_name (string): name of the tabular data resource for which you
            want to generate a Tabular Data Resource descriptor. This is the
            resource name, rather than the database table name, because we
            partition large tables into resource groups consisting of many
            files.
        datapkg_dir (path-like): The location of the directory for this
            package. The data package directory will be a subdirectory in the
            `datapkg_dir` directory, with the name of the package as the name
            of the subdirectory.
        datapkg_settings (dict): Python dictionary represeting the ETL
            parameters read in from the settings file, pertaining to the
            tabular datapackage this resource is part of.
        partitions (dict): A dictionary with PUDL database table names as the
            keys (e.g. hourly_emissions_epacems), and lists of partition
            variables (e.g. ["epacems_years", "epacems_states"]) as the keys.

    Returns:
        dict: A Python dictionary representing a tabular data resource
        descriptor that complies with the Frictionless Data specification.

    """
    # every time we want to generate the cems table, we want it compressed
    abs_path = pathlib.Path(datapkg_dir, "data", f"{resource_name}.csv")
    if "hourly_emissions_epacems" in resource_name:
        abs_path = pathlib.Path(abs_path.parent, abs_path.name + ".gz")

    # pull the skeleton of the descriptor from the megadata file
    descriptor = pull_resource_from_megadata(resource_name)
    descriptor["path"] = str(abs_path.relative_to(abs_path.parent.parent))
    descriptor["bytes"] = abs_path.stat().st_size
    descriptor["hash"] = hash_csv(abs_path)
    descriptor["created"] = (
        datetime.datetime.utcnow()
        .replace(microsecond=0)
        .isoformat() + "Z"
    )
    unpartitioned_tables = get_unpartitioned_tables([resource_name],
                                                    datapkg_settings)
    data_sources = data_sources_from_tables(unpartitioned_tables)
    descriptor["sources"] = [pc.data_source_info[src] for src in data_sources]
    descriptor["coverage"] = {
        "temporal": temporal_coverage(resource_name, datapkg_settings),
        "spatial": spatial_coverage(resource_name),
    }

    if partitions:
        for part in partitions.keys():
            if part in resource_name:
                descriptor["group"] = part

    resource = datapackage.Resource(descriptor)

    if resource.valid:
        logger.debug(f"{resource_name} is a valid resource")
    else:
        logger.info(resource)
        raise ValueError(
            f"""
            Invalid tabular data resource descriptor: {resource.name}

            Errors:
            {resource.errors}
            """
        )

    return descriptor


def compile_keywords(data_sources):
    """Compile the set of all keywords associated with given data sources.

    The list of keywords we associate with each data source is stored in
    the ``pudl.constants.keywords_by_data_source`` dictionary.

    Args:
        data_sources (iterable): List of data source codes (eia923, ferc1,
            etc.) from which to gather keywords.

    Returns:
        list: the set of all unique keywords associated with any of the input
        data sources.

    """
    keywords = set()
    for src in data_sources:
        keywords.update(pc.keywords_by_data_source[src])
    return list(keywords)


def get_autoincrement_columns(unpartitioned_tables):
    """Grab the autoincrement columns for pkg tables."""
    with importlib.resources.open_text('pudl.package_data.meta.datapkg',
                                       'datapackage.json') as md:
        metadata_mega = json.load(md)
    autoincrement = {}
    for table in unpartitioned_tables:
        try:
            autoincrement[table] = metadata_mega['autoincrement'][table]
        except KeyError:
            pass
    return autoincrement


def validate_save_datapkg(datapkg_descriptor, datapkg_dir):
    """
    Validate datapackage descriptor, save it, and validate some sample data.

    Args:
        datapkg_descriptor (dict): A Python dictionary representation of a
            (hopefully valid) tabular datapackage descriptor.
        datapkg_dir (path-like): Directory into which the datapackage.json
            file containing the tabular datapackage descriptor should be
            written.

    Returns:
        dict: A dictionary containing the goodtables datapackage validation
        report. Note that this will only be returned if there are no errors,
        otherwise it is output as an error message.

    Raises:
        ValueError: if the datapackage descriptor passed in is invalid, or if
            any of the tables has a data validation error.

    """
    # Use that descriptor to instantiate a Package object
    datapkg = datapackage.Package(datapkg_descriptor)

    # Validate the data package descriptor before we go to
    logger.info(
        f"Validating JSON descriptor for {datapkg.descriptor['name']} "
        f"tabular data package...")
    if not datapkg.valid:
        raise ValueError(
            f"Invalid tabular data package: {datapkg.descriptor['name']} "
            f"Errors: {datapkg.errors}")
    logger.info('JSON descriptor appears valid!')

    # datapkg_json is the datapackage.json that we ultimately output:
    datapkg_json = pathlib.Path(datapkg_dir, "datapackage.json")
    datapkg.save(str(datapkg_json))
    logger.info(
        f"Validating {datapkg.descriptor['name']} tabular data package "
        f"using goodtables_pandas...")
    report = goodtables.validate(str(datapkg_json))
    if not report["valid"]:
        # This will contain human-readable compact error report with up to 5 offending
        # values per problem.
        compact_report = {}
        goodtables_errors = ""
        for table in report["tables"]:
            if not table["valid"]:
                goodtables_errors += str(table["path"])
                goodtables_errors += str(table["errors"])
                compact_errors = []
                for err in table["errors"]:
                    new_err = err.copy()
                    # Only retain up to 5 samples of bad values
                    new_err["values"] = new_err["values"][:5]
                    compact_errors.append(new_err)
                compact_report[table["path"]] = compact_errors
        pretty_report = json.dumps(compact_report, sort_keys=True, indent=4)
        logger.error(f'{datapkg.descriptor["name"]} failed: {pretty_report}')
        raise ValueError(
            f"Data package data validation failed with goodtables. "
            f"Errors: {goodtables_errors}"
        )
    logger.info("Congrats! You made a valid data package!")
    logger.info("============================================================")
    logger.info("  If you like PUDL (or not!) we'd love to hear from you...")
    logger.info("  Let us know you are using PUDL at: pudl@catalyst.coop")
    logger.info("  Sign up for our newsletter: https://catalyst.coop/updates/")
    logger.info("============================================================")

    return report


def generate_metadata(datapkg_settings,
                      datapkg_resources,
                      datapkg_dir,
                      datapkg_bundle_uuid=None,
                      datapkg_bundle_doi=None):
    """
    Generate metadata for package tables and validate package.

    The metadata for this package is compiled from the pkg_settings and from
    the "megadata", which is a json file containing the schema for all of the
    possible pudl tables. Given a set of tables, this function compiles
    metadata and validates the metadata and the package. This function assumes
    datapackage CSVs have already been generated.

    See Frictionless Data for the tabular data package specification:
    http://frictionlessdata.io/specs/tabular-data-package/

    Args:
        datapkg_settings (dict): a dictionary containing package settings
            containing top level elements of the data package JSON descriptor
            specific to the data package including:
            * name: short, unique package name e.g. pudl-eia923, ferc1-test
            * title: One line human readable description.
            * description: A paragraph long description.
            * version: the version of the data package being published.
            * keywords: For search purposes.
        datapkg_resources (list): The names of tabular data resources that are
            included in this data package.
        datapkg_dir (path-like): The location of the directory for this
            package. The data package directory will be a subdirectory in the
            `datapkg_dir` directory, with the name of the package as the
            name of the subdirectory.
        datapkg_bundle_uuid: A type 4 UUID identifying the ETL run which
            which generated the data package -- this indicates that the data
            packages are compatible with each other
        datapkg_bundle_doi: A digital object identifier (DOI) that will be used
            to archive the bundle of mutually compatible data packages. Needs
            to be provided by an archiving service like Zenodo. This field may
            also be added after the data package has been generated.

    Returns:
        dict: a Python dictionary representing a valid tabular data package
        descriptor.

    """
    # Create a tabular data resource for each of the input resources:
    resources = []
    partitions = compile_partitions(datapkg_settings)
    for resource in datapkg_resources:
        resources.append(get_tabular_data_resource(
            resource,
            datapkg_dir=datapkg_dir,
            datapkg_settings=datapkg_settings,
            partitions=partitions)
        )

    datapkg_tables = get_unpartitioned_tables(
        datapkg_resources, datapkg_settings)
    data_sources = data_sources_from_tables(datapkg_tables)

    contributors = set()
    for src in data_sources:
        for c in pc.contributors_by_source[src]:
            contributors.add(c)

    # Fields which we are requiring:
    datapkg_descriptor = {
        "name": datapkg_settings["name"],
        "id": str(uuid.uuid4()),
        "profile": "tabular-data-package",
        "title": datapkg_settings["title"],
        "description": datapkg_settings["description"],
        "keywords": compile_keywords(data_sources),
        "homepage": "https://catalyst.coop/pudl/",
        "created": (datetime.datetime.utcnow().
                    replace(microsecond=0).isoformat() + 'Z'),
        "contributors": [pc.contributors[c] for c in contributors],
        "sources": [pc.data_source_info[src] for src in data_sources],
        "etl-parameters-pudl": datapkg_settings["datasets"],
        "licenses": [pc.licenses["cc-by-4.0"]],
        "autoincrement": get_autoincrement_columns(datapkg_tables),
        "python-package-name": "catalystcoop.pudl",
        "python-package-version":
            pkg_resources.get_distribution('catalystcoop.pudl').version,
        "resources": resources,
    }

    # Optional fields:
    try:
        datapkg_descriptor["version"] = datapkg_settings["version"]
    except KeyError:
        pass

    # The datapackage bundle UUID indicates packages can be used together
    if datapkg_bundle_uuid is not None:
        # Check to make sure it's a valid Type 4 UUID.
        # If it's not the right kind of hex value or string, this will fail:
        val = uuid.UUID(datapkg_bundle_uuid, version=4)
        # If it's nominally a Type 4 UUID, but these come back different,
        # something is wrong:
        if uuid.UUID(val.hex, version=4) != uuid.UUID(str(val), version=4):
            raise ValueError(
                f"Got invalid type 4 UUID: {datapkg_bundle_uuid} "
                f"as bundle ID for data package {datapkg_settings['name']}."
            )
        # Guess it looks okay!
        datapkg_descriptor["datapkg-bundle-uuid"] = datapkg_bundle_uuid

    # Check the proffered DOI, if any, against this regex, taken from the
    # idutils python package:
    if datapkg_bundle_doi is not None:
        if not pudl.helpers.is_doi(datapkg_bundle_doi):
            raise ValueError(
                f"Got invalid DOI: {datapkg_bundle_doi} "
                f"as bundle DOI for data package {datapkg_settings['name']}."
            )
        datapkg_descriptor["datapkg-bundle-doi"] = datapkg_bundle_doi

    _ = validate_save_datapkg(datapkg_descriptor, datapkg_dir)
    return datapkg_descriptor
