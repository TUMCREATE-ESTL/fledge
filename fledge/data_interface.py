"""Database interface."""

import copy
import glob
from multimethod import multimethod
import natsort
import numpy as np
import os
import pandas as pd
import sqlite3
import typing

import cobmo.data_interface
import fledge.config

logger = fledge.config.get_logger(__name__)


def recreate_database(
        additional_data_paths: typing.List[str] = fledge.config.config['paths']['additional_data']
) -> None:
    """Recreate SQLITE database from SQL schema file and CSV files in the data path / additional data paths."""

    # Connect SQLITE database (creates file, if none).
    database_connection = sqlite3.connect(fledge.config.config['paths']['database'])
    cursor = database_connection.cursor()

    # Remove old data, if any.
    cursor.executescript(
        """ 
        PRAGMA writable_schema = 1; 
        DELETE FROM sqlite_master WHERE type IN ('table', 'index', 'trigger'); 
        PRAGMA writable_schema = 0; 
        VACUUM; 
        """
    )

    # Recreate SQLITE database schema from SQL schema file.
    with open(os.path.join(fledge.config.base_path, 'fledge', 'data_schema.sql'), 'r') as database_schema_file:
        cursor.executescript(database_schema_file.read())
    database_connection.commit()

    # Import CSV files into SQLITE database.
    # - Import only from data path, if no additional data paths are specified.
    data_paths = (
        [fledge.config.config['paths']['data']] + additional_data_paths
        if additional_data_paths is not None
        else [fledge.config.config['paths']['data']]
    )
    valid_table_names = (
        pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", database_connection).iloc[:, 0].tolist()
    )
    for data_path in data_paths:
        for csv_file in glob.glob(os.path.join(data_path, '**', '*.csv'), recursive=True):

            # Exclude CSV files from CoBMo data folders.
            if (
                    (os.path.join('cobmo', 'data') not in csv_file)
                    and (os.path.join('data', 'cobmo_data') not in csv_file)
            ):

                # Debug message.
                logger.debug(f"Loading {csv_file} into database.")

                # Obtain table name.
                table_name = os.path.splitext(os.path.basename(csv_file))[0]
                # Raise exception, if table doesn't exist.
                try:
                    assert table_name in valid_table_names
                except AssertionError:
                    logger.exception(
                        f"Error loading '{csv_file}' into database, because there is no table named '{table_name}'."
                    )
                    raise

                # Load table and write to database.
                try:
                    table = pd.read_csv(csv_file, dtype=np.str)
                    table.to_sql(
                        table_name,
                        con=database_connection,
                        if_exists='append',
                        index=False
                    )
                except Exception:
                    logger.error(f"Error loading {csv_file} into database.")
                    raise

    cursor.close()
    database_connection.close()

    # Recreate CoBMo database to include FLEDGE's CoBMo definitions.
    # TODO: Modify CoBMo config instead.
    cobmo.data_interface.recreate_database(
        additional_data_paths=[
            os.path.join(fledge.config.config['paths']['data'], 'cobmo_data'),
            *fledge.config.config['paths']['cobmo_additional_data']
        ]
    )


def connect_database() -> sqlite3.Connection:
    """Connect to the database and return connection handle."""

    # Recreate database, if no database exists.
    if not os.path.isfile(fledge.config.config['paths']['database']):
        logger.debug(f"Database does not exist and is recreated at: {fledge.config.config['paths']['database']}")
        recreate_database()

    # Obtain connection handle.
    database_connection = sqlite3.connect(fledge.config.config['paths']['database'])
    return database_connection


class ScenarioData(object):
    """Scenario data object."""

    scenario: pd.Series
    timesteps: pd.Index
    parameters: pd.Series

    def __init__(
            self,
            scenario_name: str,
            database_connection=None
    ):

        # Obtain database connection.
        if database_connection is None:
            database_connection=connect_database()

        # Obtain parameters.
        self.parameters = (
            pd.read_sql(
                """
                SELECT * FROM parameters
                JOIN scenarios USING (parameter_set)
                WHERE scenario_name = ?
                """,
                con=database_connection,
                params=[scenario_name],
                index_col='parameter_name'
            ).loc[:, 'parameter_value']
        )

        # Obtain scenario data.
        scenario = (
            self.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM scenarios
                LEFT JOIN electric_grid_operation_limit_types USING (electric_grid_operation_limit_type)
                LEFT JOIN thermal_grid_operation_limit_types USING (thermal_grid_operation_limit_type)
                WHERE scenario_name = ?
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        # Raise error, if scenario not found.
        try:
            assert len(scenario) > 0
        except AssertionError:
            logger.exception(f"No scenario found for scenario name '{scenario_name}'.")
            raise
        # Convert to Series for shorter indexing.
        self.scenario = scenario.iloc[0].copy()

        # Parse time definitions.
        self.scenario['timestep_start'] = (
            pd.Timestamp(self.scenario['timestep_start'])
        )
        self.scenario['timestep_end'] = (
            pd.Timestamp(self.scenario['timestep_end'])
        )
        self.scenario['timestep_interval'] = (
            pd.Timedelta(self.scenario['timestep_interval'])
        )

        # Instantiate timestep series.
        self.timesteps = (
            pd.Index(
                pd.date_range(
                    start=self.scenario['timestep_start'],
                    end=self.scenario['timestep_end'],
                    freq=self.scenario['timestep_interval']
                ),
                name='timestep'
            )
        )

    def parse_parameters_column(
            self,
            column: np.ndarray
    ):
        """Parse parameters into one column of a dataframe.

        - Replace strings that match `parameter_name` with `parameter_value`.
        - Other strings are are directly parsed into numbers.
        - If a string doesn't match any match `parameter_name` and cannot be parsed, it is replaced with NaN.
        - Expects `column` to be passed as `np.ndarray` rather than directly as `pd.Series` (for performance reasons).
        """

        if column.dtype == object:  # `object` represents string type.
            if any(np.isin(column, self.parameters.index)):
                column_values = (
                    self.parameters.reindex(column).values
                )
                column_values[pd.isnull(column_values)] = (
                    pd.to_numeric(column[pd.isnull(column_values)])
                )
                column = column_values
            else:
                column = pd.to_numeric(column)

        # Explicitly parse to float, for consistent behavior independent of specific values.
        column = column.astype(np.float)

        return column

    def parse_parameters_dataframe(
            self,
            dataframe: pd.DataFrame,
            excluded_columns: list = None
    ):
        """Parse parameters into a dataframe.

        - Applies `parse_parameters_column` for all string columns.
        - Columns in `excluded_columns` are not parsed. By default this includes `_name`, `_type`, `connection` columns.
        """

        # Define excluded columns. By default, all columns containing the following strings are excluded:
        # `_name`, `_type`, `connection`
        if excluded_columns is None:
            excluded_columns = ['parameter_set']
        excluded_columns.extend(dataframe.columns[dataframe.columns.str.contains('_name')])
        excluded_columns.extend(dataframe.columns[dataframe.columns.str.contains('_type')])
        excluded_columns.extend(dataframe.columns[dataframe.columns.str.contains('connection')])
        excluded_columns.extend(dataframe.columns[dataframe.columns.str.contains('timestep')])

        # Select non-excluded, string columns and apply `parse_parameters_column`.
        selected_columns = (
            dataframe.columns[
                ~dataframe.columns.isin(excluded_columns)
                & (dataframe.dtypes == object)  # `object` represents string type.
            ]
        )
        for column in selected_columns:
            dataframe[column] = self.parse_parameters_column(dataframe[column].values)

        # If dataframe contains `in_service` column, remove all not-in-service elements.
        if 'in_service' in dataframe.columns:
            dataframe = dataframe.loc[dataframe.loc[:, 'in_service'] == 1, :]

        # Apply scaling.
        if 'active_power_nominal' in dataframe.columns:
            dataframe.loc[:, 'active_power_nominal'] /= (
                self.scenario.at['base_apparent_power']
            )
        if 'reactive_power_nominal' in dataframe.columns:
            dataframe.loc[:, 'reactive_power_nominal'] /= (
                self.scenario.at['base_apparent_power']
            )
        if 'resistance' in dataframe.columns:
            dataframe.loc[:, 'resistance'] *= (
                self.scenario.at['base_apparent_power']
                / self.scenario.at['base_voltage'] ** 2
            )
        if 'reactance' in dataframe.columns:
            dataframe.loc[:, 'reactance'] *= (
                self.scenario.at['base_apparent_power']
                / self.scenario.at['base_voltage'] ** 2
            )
        if 'capacitance' in dataframe.columns:
            dataframe.loc[:, 'capacitance'] *= (
                self.scenario.at['base_voltage'] ** 2
                / self.scenario.at['base_apparent_power']
            )
        if 'maximum_current' in dataframe.columns:
            dataframe.loc[:, 'maximum_current'] *= (
                self.scenario.at['base_voltage']
                / self.scenario.at['base_apparent_power']
            )
        if 'voltage' in dataframe.columns:
            dataframe.loc[:, 'voltage'] /= (
                self.scenario.at['base_voltage']
            )
        if 'apparent_power' in dataframe.columns:
            dataframe.loc[:, 'apparent_power'] /= (
                self.scenario.at['base_apparent_power']
            )
        if 'enthalpy_difference_distribution_water' in dataframe.columns:
            dataframe.loc[:, 'enthalpy_difference_distribution_water'] /= (
                self.scenario.at['base_thermal_power']
            )
        if 'thermal_power_nominal' in dataframe.columns:
            dataframe.loc[:, 'thermal_power_nominal'] /= (
                self.scenario.at['base_thermal_power']
            )

        return dataframe


class ElectricGridData(object):
    """Electric grid data object."""

    scenario_data: ScenarioData
    electric_grid: pd.DataFrame
    electric_grid_nodes: pd.DataFrame
    electric_grid_ders: pd.DataFrame
    electric_grid_lines: pd.DataFrame
    electric_grid_line_types: pd.DataFrame
    electric_grid_line_types_matrices: pd.DataFrame
    electric_grid_transformers: pd.DataFrame

    def __init__(
            self,
            scenario_name: str,
            database_connection=None
    ):

        # Obtain database connection.
        if database_connection is None:
            database_connection=connect_database()

        # Obtain scenario data.
        self.scenario_data = ScenarioData(scenario_name)

        # Obtain electric grid data.
        self.electric_grid = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grids
                WHERE electric_grid_name = (
                    SELECT electric_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            )).iloc[0]
        )
        self.electric_grid_nodes = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_nodes
                WHERE electric_grid_name = (
                    SELECT electric_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_nodes.index = self.electric_grid_nodes['node_name']
        self.electric_grid_nodes = (
            self.electric_grid_nodes.reindex(index=natsort.natsorted(self.electric_grid_nodes.index))
        )
        self.electric_grid_ders = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_ders
                WHERE electric_grid_name = (
                    SELECT electric_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_ders.index = self.electric_grid_ders['der_name']
        self.electric_grid_ders = (
            self.electric_grid_ders.reindex(index=natsort.natsorted(self.electric_grid_ders.index))
        )
        self.electric_grid_lines = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_lines
                JOIN electric_grid_line_types USING (line_type)
                WHERE electric_grid_name = (
                    SELECT electric_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_lines.index = self.electric_grid_lines['line_name']
        self.electric_grid_lines = (
            self.electric_grid_lines.reindex(index=natsort.natsorted(self.electric_grid_lines.index))
        )
        self.electric_grid_line_types = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_line_types
                WHERE line_type IN (
                    SELECT line_type FROM electric_grid_lines
                    WHERE electric_grid_name = (
                        SELECT electric_grid_name FROM scenarios
                        WHERE scenario_name = ?
                    )
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_line_types.index = self.electric_grid_line_types['line_type']
        self.electric_grid_line_types_matrices = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_line_types_matrices
                WHERE line_type IN (
                    SELECT line_type FROM electric_grid_lines
                    WHERE electric_grid_name = (
                        SELECT electric_grid_name FROM scenarios
                        WHERE scenario_name = ?
                    )
                )
                ORDER BY line_type ASC, row ASC, col ASC
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_transformers = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM electric_grid_transformers
                LEFT JOIN electric_grid_transformer_types USING (transformer_type)
                WHERE electric_grid_name = (
                    SELECT electric_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.electric_grid_transformers.index = self.electric_grid_transformers['transformer_name']
        self.electric_grid_transformers = (
            self.electric_grid_transformers.reindex(index=natsort.natsorted(self.electric_grid_transformers.index))
        )


class ThermalGridData(object):
    """Thermal grid data object."""

    scenario_data: ScenarioData
    thermal_grid: pd.DataFrame
    thermal_grid_nodes: pd.DataFrame
    thermal_grid_ders: pd.DataFrame
    thermal_grid_lines: pd.DataFrame

    def __init__(
            self,
            scenario_name: str,
            database_connection=None
    ):

        # Obtain database connection.
        if database_connection is None:
            database_connection=connect_database()

        # Obtain scenario data.
        self.scenario_data = ScenarioData(scenario_name)

        self.thermal_grid = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM thermal_grids
                JOIN der_cooling_plants ON der_cooling_plants.definition_name = thermal_grids.plant_model_name
                WHERE thermal_grid_name = (
                    SELECT thermal_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            )).iloc[0]
        )
        self.thermal_grid_nodes = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM thermal_grid_nodes
                WHERE thermal_grid_name = (
                    SELECT thermal_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.thermal_grid_nodes.index = self.thermal_grid_nodes['node_name']
        self.thermal_grid_nodes = (
            self.thermal_grid_nodes.reindex(index=natsort.natsorted(self.thermal_grid_nodes.index))
        )
        self.thermal_grid_ders = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM thermal_grid_ders
                WHERE thermal_grid_name = (
                    SELECT thermal_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.thermal_grid_ders.index = self.thermal_grid_ders['der_name']
        self.thermal_grid_ders = (
            self.thermal_grid_ders.reindex(index=natsort.natsorted(self.thermal_grid_ders.index))
        )
        self.thermal_grid_lines = (
            self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                """
                SELECT * FROM thermal_grid_lines
                JOIN thermal_grid_line_types USING (line_type)
                WHERE thermal_grid_name = (
                    SELECT thermal_grid_name FROM scenarios
                    WHERE scenario_name = ?
                )
                """,
                con=database_connection,
                params=[scenario_name]
            ))
        )
        self.thermal_grid_lines.index = self.thermal_grid_lines['line_name']
        self.thermal_grid_lines = (
            self.thermal_grid_lines.reindex(index=natsort.natsorted(self.thermal_grid_lines.index))
        )


class DERData(object):
    """DER data object."""

    scenario_data: ScenarioData
    ders: pd.DataFrame
    der_definitions: typing.Dict[str, pd.DataFrame]

    @multimethod
    def __init__(
            self,
            scenario_name: str,
            database_connection=None
    ):

        # Obtain database connection.
        if database_connection is None:
            database_connection=connect_database()

        # Obtain scenario data.
        self.scenario_data = ScenarioData(scenario_name)

        # Obtain timeseries data. Shorthand for SQL commands.
        timestep_start_string = self.scenario_data.scenario.at['timestep_start'].strftime('%Y-%m-%dT%H:%M:%S')
        timestep_end_string = self.scenario_data.scenario.at['timestep_end'].strftime('%Y-%m-%dT%H:%M:%S')

        # Obtain DERs.
        # - Obtain DERs for electric grid / thermal grid separately and perform full outer join via `pandas.merge()`,
        #   due to SQLITE missing full outer join syntax.
        self.ders = (
            pd.merge(
                pd.merge(
                    self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                        """
                        SELECT * FROM electric_grid_ders
                        WHERE electric_grid_name = (
                            SELECT electric_grid_name FROM scenarios
                            WHERE scenario_name = ?
                        )
                        """,
                        con=database_connection,
                        params=[scenario_name]
                    )),
                    self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                        """
                        SELECT * FROM thermal_grid_ders
                        WHERE thermal_grid_name = (
                            SELECT thermal_grid_name FROM scenarios
                            WHERE scenario_name = ?
                        )
                        """,
                        con=database_connection,
                        params=[scenario_name]
                    )),
                    how='outer',
                    on=['der_name', 'der_type', 'der_model_name'],
                    suffixes=('_electric_grid', '_thermal_grid')
                ),
                self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                    """
                    SELECT * FROM der_models
                    WHERE (der_type, der_model_name) IN (
                        SELECT der_type, der_model_name
                        FROM electric_grid_ders
                        WHERE electric_grid_name = (
                            SELECT electric_grid_name FROM scenarios
                            WHERE scenario_name = ?
                        )
                    )
                    OR (der_type, der_model_name) IN (
                        SELECT der_type, der_model_name
                        FROM thermal_grid_ders
                        WHERE thermal_grid_name = (
                            SELECT thermal_grid_name FROM scenarios
                            WHERE scenario_name = ?
                        )
                    )
                    """,
                    con=database_connection,
                    params=[
                        scenario_name,
                        scenario_name
                    ]
                )),
                how='left',
                on=['der_type', 'der_model_name'],
            )
        )
        self.ders.index = self.ders['der_name']
        self.ders = self.ders.reindex(index=natsort.natsorted(self.ders.index))

        # Obtain unique `definition_type` / `definition_name`.
        der_definitions_unique = self.ders.loc[:, ['definition_type', 'definition_name']].drop_duplicates()
        der_definitions_unique = der_definitions_unique.dropna(subset=['definition_type'])

        # Instantiate DER definitions dictionary.
        self.der_definitions = dict.fromkeys(pd.MultiIndex.from_frame(der_definitions_unique))

        # Append `definition_index` column to DERs, for more convenient indexing into DER definitions.
        self.ders.loc[:, 'definition_index'] = (
            pd.MultiIndex.from_frame(self.ders.loc[:, ['definition_type', 'definition_name']])
        )

        # Instantiate dict for additional DER definitions, e.g. from `flexible_ev_charger`.
        additional_der_definitions = dict()

        # Load DER definitions, first for special definition types, e.g. `cooling_plant`, `flexible_ev_charger`.
        for definition_index in self.der_definitions:

            if definition_index[0] == 'cooling_plant':

                self.der_definitions[definition_index] = (
                    pd.concat([
                        self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                            """
                            SELECT * FROM thermal_grids
                            WHERE thermal_grid_name = (
                                SELECT thermal_grid_name FROM main.scenarios
                                WHERE scenario_name = ?
                            )
                            """,
                            con=database_connection,
                            params=[scenario_name]
                        )).iloc[0],
                        self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                            """
                            SELECT * FROM der_cooling_plants
                            WHERE definition_name = ?
                            """,
                            con=database_connection,
                            params=[definition_index[1]]
                        )).iloc[0]
                    ]).drop('thermal_grid_name')  # Remove `thermal_grid_name` to avoid duplicate index in `der_models`.
                )

            elif definition_index[0] == 'flexible_ev_charger':

                self.der_definitions[definition_index] = (
                    self.scenario_data.parse_parameters_dataframe(pd.read_sql(
                        """
                        SELECT * FROM der_ev_chargers
                        WHERE definition_name = ?
                        """,
                        con=database_connection,
                        params=[definition_index[1]]
                    )).iloc[0]
                )

                # Append `definition_index`, for more convenient indexing into DER definitions.
                # - Add `accumulative` flag to ensure correct interpolation / resampling behavior.
                self.der_definitions[definition_index].at['arrival_definition_index'] = (
                    self.der_definitions[definition_index].at['arrival_definition_type'] + '_accumulative',
                    self.der_definitions[definition_index].at['arrival_definition_name']
                )
                self.der_definitions[definition_index].at['departure_definition_index'] = (
                    self.der_definitions[definition_index].at['departure_definition_type'] + '_accumulative',
                    self.der_definitions[definition_index].at['departure_definition_name']
                )
                self.der_definitions[definition_index].at['occupancy_definition_index'] = (
                    self.der_definitions[definition_index].at['occupancy_definition_type'],
                    self.der_definitions[definition_index].at['occupancy_definition_name']
                )
                self.der_definitions[definition_index].at['bidirectional_definition_index'] = (
                    self.der_definitions[definition_index].at['bidirectional_definition_type'],
                    self.der_definitions[definition_index].at['bidirectional_definition_name']
                )

                # Append arrival / occupancy timeseries / schedule to additional definitions.
                additional_der_definitions.update({
                    self.der_definitions[definition_index].at['arrival_definition_index']: None,
                    self.der_definitions[definition_index].at['departure_definition_index']: None,
                    self.der_definitions[definition_index].at['occupancy_definition_index']: None,
                    self.der_definitions[definition_index].at['bidirectional_definition_index']: None
                })

        # Append additional DER definitions.
        self.der_definitions.update(additional_der_definitions)

        # Obtain required timestep frequency for schedule resampling / interpolation.
        # - Higher frequency is only used when required. This aims to reduce computational burden.
        if (
                self.scenario_data.scenario.at['timestep_interval']
                - self.scenario_data.scenario.at['timestep_interval'].floor('min')
        ).seconds != 0:
            timestep_frequency = 's'
        elif (
                self.scenario_data.scenario.at['timestep_interval']
                - self.scenario_data.scenario.at['timestep_interval'].floor('h')
        ).seconds != 0:
            timestep_frequency = 'min'
        else:
            timestep_frequency = 'h'

        # Load DER definitions, for schedule / timeseries definitions, for each `definition_name`.
        for definition_index in self.der_definitions:

            if 'timeseries' in definition_index[0]:

                der_timeseries = (
                    pd.read_sql(
                        """
                        SELECT time, value FROM der_timeseries
                        WHERE definition_name = ?
                        AND time between ? AND ?
                        """,
                        con=database_connection,
                        params=[
                            definition_index[1],
                            timestep_start_string,
                            timestep_end_string
                        ],
                        parse_dates=['time'],
                        index_col=['time']
                    )
                )

                # Resample / interpolate / fill values.
                if 'accumulative' in definition_index[0]:

                    # Resample to scenario timestep interval, using sum to aggregate. Missing values are filled with 0.
                    der_timeseries = (
                        der_timeseries.resample(
                            self.scenario_data.scenario.at['timestep_interval'],
                            origin=self.scenario_data.scenario.at['timestep_start']
                        ).sum()
                    )
                    der_timeseries = (
                        der_timeseries.reindex(self.scenario_data.timesteps)
                    )
                    # TODO: This overwrites any missing values. No warning is raised.
                    der_timeseries = der_timeseries.fillna(0.0)

                else:

                    # Resample to scenario timestep interval, using mean to aggregate. Missing values are interpolated.
                    der_timeseries = (
                        der_timeseries.resample(
                            self.scenario_data.scenario.at['timestep_interval'],
                            origin=self.scenario_data.scenario.at['timestep_start']
                        ).mean()
                    )
                    der_timeseries = (
                        der_timeseries.reindex(self.scenario_data.timesteps)
                    )
                    der_timeseries = der_timeseries.interpolate(method='linear')

                    # Backward / forward fill up to 1h to handle edge definition gaps.
                    der_timeseries = (
                        der_timeseries.bfill(
                            limit=int(pd.to_timedelta('1h') / self.scenario_data.scenario['timestep_interval'])
                        ).ffill(
                            limit=int(pd.to_timedelta('1h') / self.scenario_data.scenario['timestep_interval'])
                        )
                    )

                # If any NaN values, display warning and fill missing values.
                if der_timeseries.isnull().any().any():
                    logger.warning(
                        f"Missing values in DER timeseries definition for '{definition_index[1]}'."
                        f" Please check if appropriate timestep_start/timestep_end are defined."
                        f" Missing values are filled with 0."
                    )
                    # Fill with 0.
                    der_timeseries = (
                        der_timeseries.fillna(0.0)
                    )

                self.der_definitions[definition_index] = der_timeseries

            elif 'schedule' in definition_index[0]:

                der_schedule = (
                    pd.read_sql(
                        """
                        SELECT time_period, value FROM der_schedules
                        WHERE definition_name = ?
                        """,
                        con=database_connection,
                        params=[definition_index[1]],
                        index_col=['time_period']
                    )
                )

                # Show warning, if `time_period` does not start with '01T00:00'.
                try:
                    assert der_schedule.index[0] == '01T00:00'
                except AssertionError:
                    logger.warning(
                        f"First time period is '{der_schedule.index[0]}' in DER schedule with definition name "
                        f"'{definition_index[1]}'. Schedules should start with time period '01T00:00'. "
                        f"Please also check if using correct time period format: 'ddTHH:MM'"
                    )

                # Parse time period index.
                # - '2001-01-...' is chosen as reference timestep, because '2001-01-01' falls on a Monday.
                der_schedule.index = pd.to_datetime('2001-01-' + der_schedule.index)

                # Obtain complete schedule for all weekdays.
                der_schedule_complete = []
                for day in range(1, 8):
                    if day in der_schedule.index.day.unique():
                        der_schedule_complete.append(
                            der_schedule.loc[der_schedule.index.day == day, :]
                        )
                    else:
                        der_schedule_previous = der_schedule_complete[-1].copy()
                        der_schedule_previous.index += pd.Timedelta('1 day')
                        der_schedule_complete.append(der_schedule_previous)
                der_schedule_complete = pd.concat(der_schedule_complete)

                # Resample / interpolate / fill values to obtain complete schedule.
                if 'accumulative' in definition_index[0]:

                    # Resample to scenario timestep interval, using sum to aggregate. Missing values are filled with 0.
                    der_schedule_complete = (
                        der_schedule_complete.resample(self.scenario_data.scenario.at['timestep_interval']).sum()
                    )
                    der_schedule_complete = (
                        der_schedule_complete.reindex(
                            pd.date_range(
                                start='2001-01-01T00:00',
                                end='2001-01-07T23:59',
                                freq=self.scenario_data.scenario.at['timestep_interval']
                            )
                        )
                    )
                    der_schedule_complete = der_schedule_complete.fillna(0.0)

                    # Resample to required timestep frequency, foward-filling intermediate values.
                    # - Ensures that the correct value is used when reindexing to obtain the full timeseries,
                    #   independent of any shift between timeseries and schedule timesteps.
                    der_schedule_complete = (
                        der_schedule_complete.resample(timestep_frequency).mean()
                    )
                    der_schedule_complete = (
                        der_schedule_complete.reindex(
                            pd.date_range(
                                start='2001-01-01T00:00',
                                end='2001-01-07T23:59',
                                freq=timestep_frequency
                            )
                        )
                    )
                    der_schedule_complete = (
                        der_schedule_complete.ffill()
                    )

                else:

                    # Resample to required timestep frequency, using mean to aggregate. Missing values are interpolated.
                    der_schedule_complete = (
                        der_schedule_complete.resample(timestep_frequency).mean()
                    )
                    der_schedule_complete = (
                        der_schedule_complete.reindex(
                            pd.date_range(
                                start='2001-01-01T00:00',
                                end='2001-01-07T23:59',
                                freq=timestep_frequency
                            )
                        )
                    )
                    der_schedule_complete = der_schedule_complete.interpolate(method='linear')

                    # Forward fill to handle definition gap at the end of the schedule.
                    der_schedule_complete = (
                        der_schedule_complete.ffill()
                    )

                # Reindex / fill schedule for given timesteps.
                der_schedule_complete.index = (
                    pd.MultiIndex.from_arrays([
                        der_schedule_complete.index.weekday,
                        der_schedule_complete.index.hour
                    ] + (
                        [der_schedule_complete.index.minute] if timestep_frequency in ['s', 'min'] else []
                    ) + (
                        [der_schedule_complete.index.second] if timestep_frequency in ['s'] else []
                    ))
                )
                der_schedule = (
                    pd.DataFrame(
                        index=pd.MultiIndex.from_arrays([
                            self.scenario_data.timesteps.weekday,
                            self.scenario_data.timesteps.hour
                        ] + (
                            [self.scenario_data.timesteps.minute] if timestep_frequency in ['s', 'min'] else []
                        ) + (
                            [self.scenario_data.timesteps.second] if timestep_frequency in ['s'] else []
                        )),
                        columns=['value']
                    )
                )
                der_schedule = (
                    der_schedule_complete.reindex(der_schedule.index)
                )
                der_schedule.index = self.scenario_data.timesteps

                self.der_definitions[definition_index] = der_schedule


class PriceData(object):
    """Price data object."""

    price_sensitivity_coefficient: np.float
    price_timeseries: pd.DataFrame

    @multimethod
    def __init__(
            self,
            scenario_name: str,
            price_type='',
            database_connection=None
    ):

        # Obtain database connection.
        if database_connection is None:
            database_connection=connect_database()

        # Obtain scenario data.
        scenario_data = ScenarioData(scenario_name)

        # Obtain DER data.
        der_data = DERData(scenario_name)

        # Obtain price type.
        price_type = scenario_data.scenario.at['price_type'] if price_type == '' else price_type

        # Obtain price sensitivity coefficient.
        self.price_sensitivity_coefficient = scenario_data.scenario.at['price_sensitivity_coefficient']

        # Obtain price timeseries.
        if price_type is None:
            price_timeseries = (
                pd.Series(
                    1.0,
                    index=scenario_data.timesteps,
                    name='price_value'
                )
            )
        else:
            price_timeseries = (
                pd.read_sql(
                    """
                    SELECT * FROM price_timeseries
                    WHERE price_type = ?
                    AND time >= (
                        SELECT timestep_start FROM scenarios
                        WHERE scenario_name = ?
                    )
                    AND time <= (
                        SELECT timestep_end FROM scenarios
                        WHERE scenario_name = ?
                    )
                    """,
                    con=database_connection,
                    params=[
                        price_type,
                        scenario_name,
                        scenario_name
                    ],
                    parse_dates=['time'],
                    index_col=['time']
                ).reindex(
                    scenario_data.timesteps
                ).interpolate(
                    'ffill'
                ).bfill(  # Backward fill to handle edge definition gaps.
                    limit=int(pd.to_timedelta('1h') / scenario_data.scenario['timestep_interval'])
                ).ffill(  # Forward fill to handle edge definition gaps.
                    limit=int(pd.to_timedelta('1h') / scenario_data.scenario['timestep_interval'])
                )
            ).loc[:, 'price_value']
            # TODO: Fix price unit conversion.
            # price_timeseries *= 1.0e-3  # 1/kWh in 1/Wh.

        # Obtain price timeseries for each DER.
        prices = (
            pd.MultiIndex.from_frame(pd.concat([
                pd.DataFrame({
                    'commodity_type': 'active_power',
                    'der_type': ['source'],
                    'der_name': ['source']
                }) if pd.notnull(scenario_data.scenario.at['electric_grid_name']) else None,
                pd.DataFrame({
                    'commodity_type': 'active_power',
                    'der_type': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'electric_grid_name']), 'der_type'],
                    'der_name': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'electric_grid_name']), 'der_name']
                }),
                pd.DataFrame({
                    'commodity_type': 'reactive_power',
                    'der_type': ['source'],
                    'der_name': ['source']
                }) if pd.notnull(scenario_data.scenario.at['electric_grid_name']) else None,
                pd.DataFrame({
                    'commodity_type': 'reactive_power',
                    'der_type': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'electric_grid_name']), 'der_type'],
                    'der_name': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'electric_grid_name']), 'der_name']
                }),
                pd.DataFrame({
                    'commodity_type': 'thermal_power',
                    'der_type': ['source'],
                    'der_name': ['source']
                }) if pd.notnull(scenario_data.scenario.at['thermal_grid_name']) else None,
                pd.DataFrame({
                    'commodity_type': 'thermal_power',
                    'der_type': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'thermal_grid_name']), 'der_type'],
                    'der_name': der_data.ders.loc[pd.notnull(der_data.ders.loc[:, 'thermal_grid_name']), 'der_name']
                })
            ]))
        )
        self.price_timeseries = pd.DataFrame(0.0, index=scenario_data.timesteps, columns=prices)
        self.price_timeseries.loc[:, prices.get_level_values('commodity_type') == 'active_power'] += (
            price_timeseries.values[:, None]
            * scenario_data.scenario.at['base_apparent_power']
        )
        # TODO: Proper thermal power price definition.
        self.price_timeseries.loc[:, prices.get_level_values('commodity_type') == 'thermal_power'] += (
            price_timeseries.values[:, None]
        )

    def copy(self):

        return copy.deepcopy(self)
