"""Rebuild the real Shapefile sample from analytical UTM coordinates."""
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED

import geopandas as gpd
from shapely.geometry import box


def main():
    destination = Path(__file__).parent / "fixtures" / "sample.zip"
    with TemporaryDirectory() as workspace:
        folder = Path(workspace)
        frame = gpd.GeoDataFrame(
            {"name": ["UTM square"], "source_id": [42]},
            geometry=[box(500_000, 5_500_000, 500_100, 5_500_100)],
            crs="EPSG:32632",
        )
        frame.to_file(folder / "survey.shp", engine="pyogrio", driver="ESRI Shapefile")
        with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
            for path in sorted(folder.iterdir()):
                archive.write(path, f"survey/{path.name}")
    print(destination)


if __name__ == "__main__":
    main()
