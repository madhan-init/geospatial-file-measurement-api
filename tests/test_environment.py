from pathlib import Path
from zipfile import ZipFile

import pyogrio
from shapely.geometry import box


FIXTURES = Path(__file__).parent / "fixtures"


def test_real_drivers_and_sample_formats(tmp_path):
    drivers = pyogrio.list_drivers(read=True)
    assert "KML" in drivers or "LIBKML" in drivers
    assert "ESRI Shapefile" in drivers
    layers = pyogrio.list_layers(FIXTURES / "sample.kml")
    assert {name for name, kind in layers if kind} == {"plots", "routes"}
    frames = [pyogrio.read_dataframe(FIXTURES / "sample.kml", layer=name,
              force_2d=True, use_arrow=False, on_invalid="warn") for name, kind in layers if kind]
    assert sum(len(frame) for frame in frames) == 3
    assert all(frame.crs.to_epsg() == 4326 for frame in frames)
    assert [geometry.geom_type for frame in frames for geometry in frame.geometry] == ["Polygon", "LineString", "Point"]
    with ZipFile(FIXTURES / "sample.zip") as archive:
        # This trusted fixture is generated locally; uploaded archives use the bounded reader.
        archive.extractall(tmp_path)
    frame = pyogrio.read_dataframe(tmp_path / "survey/survey.shp", force_2d=True,
                                   use_arrow=False, on_invalid="warn")
    assert len(frame) == 1
    assert frame.crs.to_epsg() == 32632
    assert frame.geometry.iloc[0].equals(box(500_000, 5_500_000, 500_100, 5_500_100))
    assert frame.iloc[0]["source_id"] == 42
