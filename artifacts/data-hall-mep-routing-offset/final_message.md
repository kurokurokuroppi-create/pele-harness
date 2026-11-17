**Deliverables**

1. **Script**: `output/solution.py`  
   - A self-contained Python script using `ifcopenshell==0.8.4` and `numpy` to rebuild the hall, route MEP services, and export an IFC4 model.  
   - Hardcoded data from the seven drawing sheets (positions, sizes, elevations) baked into literals.  
   - Implements clash detection, code compliance (clearances, separations, headroom), and service routing constraints.  
   - Includes diagnostics for clearances, under-floor/overhead splits, and service elevations.  

2. **Model**: `output/model.ifc`  
   - Valid IFC4 file with:  
     - Racks on floor tiles, structural columns, CRAHs, PDUs, sprinklers, and containment.  
     - Three overhead services (tray, busway, chilled water) routed clash-free, serving all rows and CRAHs.  
     - Pset_DCM_* property sets, correct element classes, and spatial structure.  
     - Express rules validation (`express_rules=True`).  

3. **Reconciliation Log** (optional): `output/reconciliation.json`  
   - JSON log of numeric notes extracted from drawings (e.g., "A-301 clear height 2600 mm -> enforced as headroom rule").  

**Key Results**  
- **Clash-free routing**: Services avoid columns, racks, and sprinklers (≥2 mm separation).  
- **Code compliance**: 2600 mm headroom, 460 mm sprinkler clearance, 300 mm tray-busway separation.  
- **Connectivity**: All rows served ≥80% by each service; chilled water reaches all CRAHs.  
- **Diagnostics**: Clearances, under-floor/overhead splits, and elevation bands measured and stored.  

Both files exist, are non-empty, and validated. The script runs deterministically within 300 seconds.