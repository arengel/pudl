"""tests for pudl/output/epacems.py loading functions."""
from pathlib import Path

import dask.dataframe as dd
import pytest

from pudl.output.epacems import epacems


@pytest.fixture(scope='module')
def epacems_year_and_state(etl_settings):
    """Find the year and state defined in pudl/package_data/settings/etl_*.yml."""
    # the etl_settings data structure alternates dicts and lists so indexing is a pain.
    return etl_settings.datasets.epacems


@pytest.fixture(scope='session')
def epacems_parquet_path(
    pudl_settings_fixture,
    pudl_engine,  # implicit dependency; ensures .parquet files exist
):
    """Get path to the directory of EPA CEMS .parquet data."""
    out_dir = Path(pudl_settings_fixture['parquet_dir'], 'epacems')
    return out_dir


def test_epacems_subset(epacems_year_and_state, epacems_parquet_path):
    """Minimal integration test of epacems(). Check if it returns a DataFrame."""
    if not epacems_year_and_state:
        pytest.skip("EPA CEMS not in settings file and so is not being tested.")
    path = epacems_parquet_path
    years = epacems_year_and_state.years
    # Use only Idaho if multiple states are given
    states = epacems_year_and_state.states if len(
        epacems_year_and_state.states) == 1 else ['ID']
    actual = epacems(columns=["gross_load_mw"],
                     epacems_path=path,
                     years=years,
                     states=states)
    assert isinstance(actual, dd.DataFrame)
    assert actual.shape[0].compute() > 0  # n rows


def test_epacems_subset_input_validation(epacems_year_and_state, epacems_parquet_path):
    """Check if invalid inputs raise exceptions."""
    if not epacems_year_and_state:
        pytest.skip("EPA CEMS not in settings file and so is not being tested.")
    path = epacems_parquet_path
    valid_year = epacems_year_and_state.years[-1]
    valid_state = epacems_year_and_state.states[-1]
    valid_column = "gross_load_mw"

    invalid_state = 'confederacy'
    invalid_year = 1775
    invalid_column = 'clean_coal'
    combos = [
        dict(
            years=[valid_year],
            states=[valid_state],
            columns=[invalid_column],
        ),
        dict(
            years=[valid_year],
            states=[invalid_state],
            columns=[valid_column],
        ),
        dict(
            years=[invalid_year],
            states=[valid_state],
            columns=[valid_column],
        ),
    ]
    for combo in combos:
        with pytest.raises(ValueError):
            epacems(epacems_path=path, **combo)
