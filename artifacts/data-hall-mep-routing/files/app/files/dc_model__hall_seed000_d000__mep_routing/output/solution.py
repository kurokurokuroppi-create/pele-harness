import ifcopenshell
import numpy as np

# Hardcoded drawing data (example positions, sizes, elevations)
racks = [{'x': 1000, 'y': 2000, 'z': 100, 'size': (1200, 800)}, ...]
structural_columns = [...] # 27x21m grid
 crahs = [{'x': 1500, 'y': 3000, 'z': 150, 'size': (600, 400)}, ...]
 pdus = [{'x': 1000, 'y': 2500, 'z': 100, 'size': (400, 200)}, ...]
 access_floor = {'z': 0, 'height': 200}
 containment = {'z': 200, 'height': 1000}
 sprinklers = [{'x': 1200, 'y': 2200, 'z': 100, 'deflector_height': 150}, ...]

# Create IFC model
model = ifcopenshell.file(schema='IFC4')

# Add structural elements
for col in structural_columns:
    col_elem = model.createIfcColumn(...)
    # Set properties and placements

# Add racks
for rack in racks:
    rack_elem = model.createIfcFurniture(...)
    rack_elem.Name = 'Rack'
    # Set Pset_DCM_Rack properties

# Add CRAHs
for crah in crahs:
    crah_elem = model.createIfcFurniture(...)
    crah_elem.Name = 'CRAH'
    # Set Pset_DCM_Sprinkler properties

# Add PDUs
for pdu in pdus:
    pdu_elem = model.createIfcDistributionPort(...)
    # Set properties

# Add access floor
access_floor_elem = model.createIfcManhole(...)

# Add containment
containment_elem = model.createIfcBuildingElementProxy(...)

# Add sprinklers
for sprinkler in sprinklers:
    sprinkler_elem = model.createIfcFlowController(...)
    # Set properties

# Route services
# Telecom tray
tray_segments = [...] # Axis-aligned segments
for seg in tray_segments:
    tray_elem = model.createIfcElectricTray(...)
    # Set placement and properties

# Busway
busway_segments = [...] # Axis-aligned segments
for seg in busway_segments:
    busway_elem = model.createIfcElectricConductingPipe(...)
    # Set placement and properties

# Chilled water
chw_segments = [...] # Axis-aligned segments
for seg in chw_segments:
    chw_elem = model.createIfcPipe(...)
    # Set placement and properties

# Clash checks
for elem1, elem2 in all_element_pairs:
    if ifcopenshell.geom.is_collision(elem1, elem2, tolerance=2):
        raise ValueError("Clash detected!")

# Code compliance checks
# Headroom, clearances, separations, etc.

# Export IFC
model.write("output/model.ifc")
