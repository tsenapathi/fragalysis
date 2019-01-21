#!/usr/bin/env python
# coding=utf-8

"""process_molport_compounds.py

Processes MolPort vendor compound files, expected to contain pricing
information, against the colated graph processing files.

Note:   This module does expect `colate_all` to have been used on the original
        graph files to produce normalised supplier identities in the node file
        this module uses.

For the graph design refer to the Google-Drive graph model document at...

    https://drive.google.com/file/d/1g4jT3yhwQYqsKwMpE3fYAA7dgGBYhBiw

The files generated (in a named output directory) are:

-   "molport-suppliermol-nodes.csv.gz"
    containing nodes that define the unique set of compound IDs
    and their original smiles.

-   "molport-supplier-nodes.csv.gz"
    containing the supplier node(s) for the vendor.

-   "molport-suppliermol-supplier-edges.csv.gz"
    containing the "SupplierMol" to "Supplier"
    relationships using the the type of "Availability".

Every fragment line that has a MolPort identifier in the original data set
is labelled and a relationship created between it and the Vendor's compound(s).
The compounds are also related to purchasing costs for those compounds in
various "pack sizes".

Some vendor compound nodes may have no defined costs and some compounds may
not exist in the original data set.

The files generated (in a named output directory) are:

-   "molport-isomol-nodes.csv.gz"
    containing information about compounds that are isomeric.

-   "molport-molecule-suppliermol-edges.csv.gz"
    containing the relationships between the original fragment node entries
    and the Vendor "Compound" nodes (where the compounds not isomeric)

-   "molport-isomol-molecule-edges.csv.gz"
    containing the relationships between IsoMol entries and
    the fragment nodes (where the fragment is isomeric).

The module augments the original nodes by adding the label
"Mol" and "MolPort" for all MolPort compounds that have been found
to the augmented copy of the original node file that it creates.

If the original nodes file is "nodes.csv.gz" the augmented copy
(in the named output directory) will be called
"molport-augmented-nodes.csv.gz".

Note:   At the moment the original nodes.csv.gz file is expected to contain
        standardised (uppercase) compound identifiers,
        i.e. "MOLPORT:NNN-NNN-NNN" whereas the compound files (that include
        pricing information) are expected to use the supplier's identifier,
        i.e. "MolPort-NNN-NNN-NNN". See the 'molport_re' and 'supplier_prefix'
        variables in this module.

        In the future the standardiser should produce a new compound file
        that contains all the relevant columns passed through. At the moment
        it just contains SSMILES, OSMILES and ID columns.

-   "molport-unknown-fragment-compounds.txt"
    is a file that contains vendor compounds referred to in the fragment file
    that are not in the Vendor data.

Alan Christie
January 2019
"""

import argparse
from collections import namedtuple
import glob
import gzip
import logging
import os
import re
import sys

from rdkit import Chem
from rdkit import RDLogger
from frag.utils.rdkit_utils import standardize

# Configure basic logging
logger = logging.getLogger('molport')
out_hdlr = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s %(levelname)s # %(message)s',
                              '%Y-%m-%dT%H:%M:%S')
out_hdlr.setFormatter(formatter)
out_hdlr.setLevel(logging.INFO)
logger.addHandler(out_hdlr)
logger.setLevel(logging.INFO)

# The minimum number of columns in the input data and
# a map of expected column names indexed by column number.
#
# The molecule data is spread over a number of `txt.gz` files
# (i.e. files like `iis_smiles-000-000-000--000-499-999.txt.gz`)
# in a common directory where the files have the following header
# names and (0-based) positions:
#
# SMILES                0
# SMILES_CANONICAL      1
# MOLPORTID             2
# STANDARD_INCHI        3
# INCHIKEY              4
# PRICERANGE_1MG        5
# PRICERANGE_5MG        6
# PRICERANGE_50MG       7
# BEST_LEAD_TIME        8

expected_min_num_cols = 9
smiles_col = 0
compound_col = 2
cost_col = {1: 5, 5: 6, 50: 7}
blt_col = 8
expected_input_cols = {smiles_col: 'SMILES',
                       compound_col: 'MOLPORTID',
                       cost_col[1]: 'PRICERANGE_1MG',
                       cost_col[5]: 'PRICERANGE_5MG',
                       cost_col[50]: 'PRICERANGE_50MG',
                       blt_col: 'BEST_LEAD_TIME'}

# The Vendor SupplierMol node has...
# a compound id (unique for a given vendor)
# a SMILES string
SupplierMolNode = namedtuple('SupplierMol', 'cmpd_id osmiles')
# The Vendor Supplier node has...
# a name
SupplierNode = namedtuple('Supplier', 'name')
# The Cost node has...
# a pack size (mg)
# a minimum price
# a maximum price
CostNode = namedtuple('CostNode', 'ps min max')

# Map of Vendor compounds that are isomeric, and their standard representation.
# The index is a Vendor compound ID and the value is the standardised form.
# If the compound is in this map it is isometric.
compound_isomer_map = {}
# Map of standardised SMILES to vendor compound(s)
# that have isomeric representations.
# The index is standardised (isomeric) SMILES
# and the value is a set() of Vendor compound IDs
isomol_smiles = {}
# Map of non-isomeric SMILES representations to isomeric smiles
# (where the molecule is isomeric). This helps lookup
# Vendor molecules that are isomeric rather than using the
# Vendor's compound ID.
nonisomol_smiles = {}
# All the vendor compound IDs
vendor_compounds = set()
# The set of all vendor compounds found in the fragment line
# where a Vendor compound was not found.
unknown_vendor_compounds = set()

# Prefix for output files
output_filename_prefix = 'molport'
# The namespaces of the various indices
frag_namespace = 'F2'
suppliermol_namespace = 'SM_MP'
supplier_namespace = 'S'
isomol_namespace = 'ISO'

# Regular expression to find the MolPort compound IDs
# (in the original nodes file).
molport_re = re.compile(r'MOLPORT:(\d+-\d+-\d+)')
# The compound identifier prefix
# the vendor uses in the the compound files...
supplier_prefix = 'MolPort-'
# The prefix we use in our fragment file
# and the prefix we use for our copy of the
molport_prefix = 'MOLPORT:'

# Various diagnostic counts
num_nodes = 0
num_nodes_augmented = 0
num_compound_relationships = 0
num_compound_iso_relationships = 0
num_vendor_iso_mols = 0
num_vendor_mols = 0
num_vendor_molecule_failures = 0

# The line rate at which the augmenter writes updates to stdout.
# Every 20 million?
augment_report_rate = 20000000


def error(msg):
    """Prints an error message and exists.

    :param msg: The message to print
    """
    logger.error('ERROR: %s', msg)
    sys.exit(1)


def create_cost_node(pack_size, field_value):
    """Creates a CostNode namedtuple for the provided pack size
    and corresponding pricing field. If the pricing field
    is empty or does not correspond to a recognised format
    or has no min or max value no CostNode is created.

    :param pack_size: The pack size (mg). Typically 1, 5, 50 etc.
    :param field_value: The pricing field value, e.g. "100 - 500"
    :returns: A CostNode namedtuple (or None if no pricing)
    """

    # The cost/pricing field value
    # has a value that is one of:
    #
    # "min - max"   e.g. "50 - 100"
    # "< max"       e.g. "< 1000"
    # "> min"       e.g. "> 50"

    min_val = None
    max_val = None
    c_node = None
    if field_value.startswith('>'):
        min_val = float(field_value.split()[1])
    elif field_value.startswith('<'):
        max_val = float(field_value.split()[1])
    elif ' - ' in field_value:
        min_val = float(field_value.split(' - ')[0])
        max_val = float(field_value.split(' - ')[1])

    if min_val is not None or max_val is not None:
        c_node = CostNode(pack_size, min_val, max_val)

    return c_node


def extract_vendor_compounds(suppliermol_gzip_file,
                             suppliermol_edges_gzip_file,
                             supplier_id,
                             gzip_filename):
    """Process the given file and extract vendor (and pricing) information.
    Vendor nodes are only created when there is at least one
    column of pricing information.

    This method extracts vendor information and writes the following files: -

    -   "molport-suppliermol-nodes.csv.gz"
    -   "molport-suppliermol-supplier-edges.csv.gz"

    The following files are expected to be written elsewhere: -

    -   "molport-supplier-nodes.csv.gz"

    The "ID" in the SupplierMol nodes file is the Compound ID and the
    "ID" of the (single) Supplier node is the supplier Name.

    As we load the Vendor compounds we 'standardise' the SMILES and
    determine whether they represent an isomer or not.

    :param suppliermol_gzip_file: The SupplierMol node file
    :param suppliermol_edges_gzip_file: The SupplierMol to Supplier edges file
    :param supplier_id: The ID of the supplier node
    :param gzip_filename: The compressed file to process
    """

    global compound_isomer_map
    global isomol_smiles
    global nonisomol_smiles
    global num_vendor_iso_mols
    global num_vendor_mols
    global num_vendor_molecule_failures

    logger.info('Processing %s...', gzip_filename)

    num_lines = 0
    with gzip.open(gzip_filename, 'rt') as gzip_file:

        # Check first line (a space-delimited header).
        # This is a basic sanity-check to make sure the important column
        # names are what we expect.

        hdr = gzip_file.readline()
        field_names = hdr.split('\t')
        # Expected minimum number of columns...
        if len(field_names) < expected_min_num_cols:
            error('expected at least {} columns found {}'.
                  format(expected_input_cols, len(field_names)))
        # Check salient columns...
        for col_num in expected_input_cols:
            if field_names[col_num].strip() != expected_input_cols[col_num]:
                error('expected "{}" in column {} found "{}"'.
                      format(expected_input_cols[col_num],
                             col_num,
                             field_names[col_num]))

        # Columns look right...

        for line in gzip_file:

            num_lines += 1
            fields = line.split('\t')
            if len(fields) <= 1:
                continue

            o_smiles = fields[smiles_col]
            compound_id = molport_prefix + fields[compound_col].split(supplier_prefix)[1]
            blt = int(fields[blt_col].strip())

            # Add the compound (expected to be unique)
            # to our set of 'all compounds'.
            if compound_id in vendor_compounds:
                error('Duplicate compound ID ({})'.format(compound_id))
            vendor_compounds.add(compound_id)

            # Standardise and update global maps...
            # And try and handle and report any catastrophic errors
            # from dependent modules/functions.

            mol = None
            std = None
            iso = None
            noniso = None

            try:
                mol = Chem.MolFromSmiles(o_smiles)
            except Exception as e:
                logger.warning('MolFromSmiles(%s) exception: "%s"', o_smiles, e.message)
            if not mol:
                num_vendor_molecule_failures += 1
                logger.error('Got nothing from MolFromSmiles(%s).'
                             ' Skipping this Vendor compound'
                             ' (id=%s line=%s failures=%s)',
                             o_smiles, compound_id, num_lines,
                             num_vendor_molecule_failures)
                continue

            # Got a molecule.
            #
            # Try to (safely) standardise,
            # and create isomeric an non-isomeric representations.

            try:
                std = standardize(mol)
            except Exception as e:
                logger.warning('standardize(%s) exception: "%s"', o_smiles, e.message)
            if not std:
                num_vendor_molecule_failures += 1
                logger.error('Got nothing from standardize(%s).'
                             ' Skipping this Vendor compound'
                             ' (line=%s failures=%s)',
                             o_smiles, num_lines, num_vendor_molecule_failures)
                continue

            try:
                iso = Chem.MolToSmiles(std, isomericSmiles=True, canonical=True)
            except Exception as e:
                logger.warning('MolToSmiles(%s, iso) exception: "%s"', o_smiles, e.message)
            if not iso:
                num_vendor_molecule_failures += 1
                logger.error('Got nothing from MolToSmiles(%s, iso).'
                             ' Skipping this Vendor compound'
                             ' (line=%s failures=%s)',
                             o_smiles, num_lines, num_vendor_molecule_failures)
                continue

            try:
                noniso = Chem.MolToSmiles(std, isomericSmiles=False, canonical=True)
            except Exception as e:
                logger.warning('MolToSmiles(%s, noniso) exception: "%s"', o_smiles, e.message)
            if not noniso:
                num_vendor_molecule_failures += 1
                logger.error('Got nothing from MolToSmiles(%s, noniso).'
                             ' Skipping this Vendor compound'
                             ' (line=%s failures=%s)',
                             o_smiles, num_lines, num_vendor_molecule_failures)
                continue

            # Is it isomeric?
            num_vendor_mols += 1
            if iso != noniso:
                num_vendor_iso_mols += 1
                if iso not in isomol_smiles:
                    # This standardised SMILES is not
                    # in the map of existing isomers
                    # so start a new list of customer compounds...
                    isomol_smiles[iso] = set(compound_id)
                else:
                    # Standard SMILES already
                    isomol_smiles[iso].add(compound_id)
                compound_isomer_map[compound_id] = iso
                # Put a lookup of iso representation from the non-iso
                if noniso not in nonisomol_smiles:
                    nonisomol_smiles[noniso] = set(iso)
                else:
                    nonisomol_smiles[noniso].add(iso)

            # Write the SupplierMol entry
            suppliermol_gzip_file.write('{},"{}",Available\n'.
                                        format(compound_id,
                                               o_smiles))

            # And add suitable 'Availability' relationships with the Supplier
            for quantity in [1, 5, 50]:
                cost = create_cost_node(quantity, fields[cost_col[quantity]])
                if cost:
                    cost_min = str(cost.min) if cost.min else ''
                    cost_max = str(cost.max) if cost.max else ''
                    suppliermol_edges_gzip_file.\
                        write('{},{},{},{},USD,{},{},Availability\n'.
                              format(compound_id,
                                     quantity,
                                     cost_min,
                                     cost_max,
                                     blt,
                                     supplier_id))


def write_isomol_nodes(directory, isomol_smiles):
    """Writes the IsoMol nodes file, including a header.

    :param directory: The sub-directory to write to
    :param isomol_smiles: A map of standard SMILES to a list of compounds
    """

    filename = os.path.join(directory,
                            '{}-isomol-nodes.csv.gz'.
                            format(output_filename_prefix))
    logger.info('Writing %s...', filename)

    num_nodes = 0
    with gzip.open(filename, 'wb') as gzip_file:
        gzip_file.write('smiles:ID({}),'
                        'cmpd_ids:STRING[],'
                        ':LABEL\n'.format(isomol_namespace))
        for smiles in isomol_smiles:
            # Construct the 'array' of compounds (';'-separated)
            compound_ids = isomol_smiles[smiles][0]
            for compound_id in isomol_smiles[smiles][1:]:
                compound_ids += ';{}'.format(compound_id)
            # Write the row...
            gzip_file.write('"{}",{},CanSmi;Mol;MolPort\n'.
                            format(smiles, compound_ids))
            num_nodes += 1

    logger.info(' {:,}'.format(num_nodes))


def write_supplier_nodes(directory, supplier_id):
    """Writes the IsoMol nodes file, including a header.

    :param directory: The sub-directory to write to
    :param supplier_id: The supplier
    """

    filename = os.path.join(directory,
                            '{}-supplier-nodes.csv.gz'.
                            format(output_filename_prefix))
    logger.info('Writing %s...', filename)

    with gzip.open(filename, 'wb') as gzip_file:
        gzip_file.write('name:ID({}),'
                        ':LABEL\n'.format(supplier_namespace))
        # Write the solitary row...
        gzip_file.write('"{}",Supplier\n'.format(supplier_id))

    logger.info(' 1')


def write_isomol_suppliermol_relationships(directory, isomol_smiles):
    """Writes the IsoMol to SupplierMol relationships file, including a header.

    :param directory: The sub-directory to write to
    :param isomol_smiles: The map of standardised SMILES
                          to a list of Vendor compound IDs
    """

    filename = os.path.join(directory,
                            '{}-isomol-suppliermol-edges.csv.gz'.
                            format(output_filename_prefix))
    logger.info('Writing %s...', filename)

    num_edges = 0
    with gzip.open(filename, 'wb') as gzip_file:
        gzip_file.write(':START_ID({}),'
                        ':END_ID({}),'
                        ':TYPE\n'.format(isomol_namespace, suppliermol_namespace))
        for smiles in isomol_smiles:
            for compound_id in isomol_smiles[smiles]:
                gzip_file.write('"{}",{},HasVendor\n'.format(smiles, compound_id))
                num_edges += 1

    logger.info(' {:,}'.format(num_edges))


def augment_colated_nodes(directory, filename, has_header):
    """Augments the original nodes file and writes the relationships
    for nodes in this file to the Vendor nodes.
    """

    global num_nodes
    global num_nodes_augmented
    global num_compound_relationships
    global num_compound_iso_relationships
    global unknown_vendor_compounds
    global isomol_smiles
    global nonisomol_smiles

    logger.info('Augmenting %s as...', filename)

    # Augmented file
    augmented_filename = \
        os.path.join(directory,
                     '{}-augmented-{}.gz'.format(output_filename_prefix,
                                                 os.path.basename(filename)))
    gzip_ai_file = gzip.open(augmented_filename, 'wt')
    # Frag to SupplierMol relationships file
    augmented_noniso_relationships_filename = \
        os.path.join(directory,
                     '{}-molecule-suppliermol-edges.csv.gz'.
                     format(output_filename_prefix))
    gzip_smr_file = gzip.open(augmented_noniso_relationships_filename, 'wt')
    gzip_smr_file.write(':START_ID({}),'
                        ':END_ID({}),'
                        ':TYPE\n'.format(frag_namespace, suppliermol_namespace))

    # IsoMol to Frag relationships file
    augmented_iso_relationships_filename = \
        os.path.join(directory,
                     '{}-isomol-molecule-edges.csv.gz'.
                     format(output_filename_prefix))
    gzip_ifr_file = gzip.open(augmented_iso_relationships_filename, 'wt')
    gzip_ifr_file.write(':START_ID({}),'
                        ':END_ID({}),'
                        ':TYPE\n'.format(isomol_namespace, frag_namespace))

    logger.info(' %s', augmented_filename)
    logger.info(' %s', augmented_noniso_relationships_filename)
    logger.info(' %s', augmented_iso_relationships_filename)

    with open(filename, 'rt') as n_file:

        if has_header:
            # Copy first line (header)
            hdr = n_file.readline()
            gzip_ai_file.write(hdr)

        for line in n_file:

            num_nodes += 1
            # Give user a gentle reminder to stdout
            # that all is progressing...
            if num_nodes % augment_report_rate == 0:
                logger.info(' ...at fragment {:,} ({:,}/{:,})'.
                            format(num_nodes,
                                   num_compound_relationships,
                                   num_compound_iso_relationships))

            # Check thew fragments's SMILES against our nonisomol map.
            # This is a map into our IsoMol table ansd is a surrogate
            # for the lack of isomeric compound IDs that the colate
            # utility should insert (but doesn't).
            #
            # Then search for MolPort compound identities on the line.

            need_to_augment = False
            frag_smiles = line.split(',')[0]

            if frag_smiles in nonisomol_smiles:

                # We've found the fragment (non-iso) SMILES in map
                # that indicates it's a non-isomeric representation
                # of an isomer. We should augment the entry.
                for noniso_isomol_smiles in nonisomol_smiles[frag_smiles]:

                    # A relationship from IsoMol to Frag.
                    gzip_ifr_file.write('"{}","{}",NonIso\n'.
                                        format(noniso_isomol_smiles,
                                               frag_smiles))

                    # A relationship (or relationships)
                    # from Frag to SupplierMol
                    for molport_compound_id in isomol_smiles[noniso_isomol_smiles]:
                        gzip_smr_file.write('"{}",{},HasVendor\n'.
                                            format(frag_smiles,
                                                   molport_compound_id))

                need_to_augment = True
                num_compound_iso_relationships += 1
                num_compound_relationships += 1

            # We've looked up the SMILES string.
            # Now search for compound IDs (that will be non-isomeric)
            # on the fragment line...

            match_ob = molport_re.findall(line)
            if match_ob:
                # Append a relationship in the fragment-suppliermol-edges
                # file to the SupplierMol if a Vendor compound has been found.
                # Do this for each compound that was found...
                for compound_id in match_ob:
                    molport_compound_id = molport_prefix + compound_id
                    if molport_compound_id in vendor_compounds:
                        # A relationship from Frag to SupplierMol
                        gzip_smr_file.write('"{}",{},HasVendor\n'.
                                            format(frag_smiles,
                                                   molport_compound_id))

                        num_compound_relationships += 1
                        need_to_augment = True
                    else:
                        # Compound not found.
                        # Place the unaltered compound ID in a list
                        # of those not known...
                        unknown_vendor_compounds.add(compound_id)

            if need_to_augment:
                # Augment the fragment entry...
                new_line = line.strip() + ';CanSmi;Mol;V_MP\n'
                gzip_ai_file.write(new_line)
                num_nodes_augmented += 1
            else:
                # No compounds for this line,
                # just write it out 'as-is'
                gzip_ai_file.write(line)

    # Close augmented nodes and the relationships
    gzip_ai_file.close()
    gzip_smr_file.close()
    gzip_ifr_file.close()


if __name__ == '__main__':

    parser = argparse.ArgumentParser('Vendor Compound Processor (MolPort)')
    parser.add_argument('vendor_dir',
                        help='The MolPort vendor directory,'
                             ' containing the ".gz" files to be processed.')
    parser.add_argument('vendor_prefix',
                        help='The MolPort vendor file prefix,'
                             ' i.e. "iis_smiles". Only files with this prefix'
                             ' in the vendor directory will be processed')
    parser.add_argument('nodes',
                        help='The uncompressed nodes file to augment with'
                             ' the collected vendor data')
    parser.add_argument('output',
                        help='The output directory')
    parser.add_argument('--nodes-has-header',
                        help='Use if the nodes file has a header',
                        action='store_true')

    args = parser.parse_args()

    # Create the output directory
    if not os.path.exists(args.output):
        os.mkdir(args.output)
    if not os.path.isdir(args.output):
        error('output ({}) is not a directory'.format(args.output))

    # -------
    # Stage 1 - Process Vendor Files
    # -------

    # Suppress basic RDKit logging...
    RDLogger.logger().setLevel(RDLogger.ERROR)

    # Open new files for writing.
    #
    # The output files are: -
    # - One for the SupplierMol nodes
    # - And one for the relationships to the (expected) supplier node.
    # - And one for the imomeric molecules.
    suppliermol_filename = os.path.\
        join(args.output,
             '{}-suppliermol-nodes.csv.gz'.
             format(output_filename_prefix))
    logger.info('Writing %s...', suppliermol_filename)
    suppliermol_gzip_file = gzip.open(suppliermol_filename, 'wt')
    suppliermol_gzip_file.write('cmpd_id:ID({}),'
                                'osmiles,'
                                ':LABEL\n'.format(suppliermol_namespace))

    suppliermol_edges_filename = os.path.\
        join(args.output,
             '{}-suppliermol-supplier-edges.csv.gz'.
             format(output_filename_prefix))
    logger.info('Writing %s...', suppliermol_edges_filename)
    suppliermol_edges_gzip_file = gzip.open(suppliermol_edges_filename, 'wt')
    suppliermol_edges_gzip_file.write(':START_ID({}),'
                                      'quantity,'
                                      'price_min,'
                                      'price_max,'
                                      'currency,'
                                      'lead_time,'
                                      ':END_ID({}),'
                                      ':TYPE\n'.format(suppliermol_namespace,
                                                       supplier_namespace))

    # Process all the Vendor files...
    molport_files = glob.glob('{}/{}*.gz'.format(args.vendor_dir,
                                                 args.vendor_prefix))
    for molport_file in molport_files:
        extract_vendor_compounds(suppliermol_gzip_file,
                                 suppliermol_edges_gzip_file,
                                 'MolPort', molport_file)

    # Close the SupplierMol and the edges file.
    suppliermol_gzip_file.close()
    suppliermol_edges_gzip_file.close()

    # Write the supplier node file...
    write_supplier_nodes(args.output, 'MolPort')

    # -------
    # Stage 2 - Write the IsoMol nodes
    # -------
    # We have collected and written SupplierMol nodes, Supplier nodes
    # and relationships and have a map of the vendor molecules
    # that are isomeric.

    write_isomol_nodes(args.output, isomol_smiles)
    write_isomol_suppliermol_relationships(args.output, isomol_smiles)

    # -------
    # Stage 3 - Augment
    # -------
    # Augment the processed nodes file
    # and attach relationships between it, the IsoMol and SupplierMol nodes.
    # This stage: -
    # - Augments the colated Nodes file
    # - Creates up to 2 new relationships between:
    #   - IsoMol and Fragment Network
    #   - Fragment Network and SupplierMol

    augment_colated_nodes(args.output, args.nodes, has_header=args.nodes_has_header)

    # Summary
    logger.info('{:,}/{:,} vendor molecules/iso'.format(num_vendor_mols, num_vendor_iso_mols))
    logger.info('{:,} vendor molecule failures'.format(num_vendor_molecule_failures))
    logger.info('{:,}/{:,} nodes/augmented'.format(num_nodes, num_nodes_augmented))
    logger.info('{:,}/{:,} node compound relationships/iso'.format(num_compound_relationships, num_compound_iso_relationships))

    # Dump compounds that were referenced in the fragment file
    # but not found in the vendor data.
    # Or remove any file that might already exist.
    unknown_vendor_compounds_file_name = os.path.join(args.output,
                             '{}-unknown_vendor_compounds.txt'.
                             format(output_filename_prefix))
    if unknown_vendor_compounds:
        file_name = os.path.join(args.output,
                                 '{}-unknown_vendor_compounds.txt'.
                                 format(output_filename_prefix))
        logger.info('{:,} unknown compounds (see {})'.
                    format(len(unknown_vendor_compounds),
                           unknown_vendor_compounds_file_name))
        with open(unknown_vendor_compounds_file_name, 'wt') as unknown_vendor_compounds_file:
            for unknown_vendor_compound in unknown_vendor_compounds:
                unknown_vendor_compounds_file.write(unknown_vendor_compound + '\n')
    else:
        logger.info('0 unknown compounds')
        if os.path.exists(unknown_vendor_compounds_file_name):
            os.remove(unknown_vendor_compounds_file_name)
