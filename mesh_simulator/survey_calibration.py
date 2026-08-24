from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable

import numpy as np

from .geography import latlon_to_world
from .model import MIN_DECODE_MARGIN_DB, OBSTACLE_DEFAULTS, Node, PropagationModel, Scenario


class SurveyCalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class BuildingCalibration:
    sample_count: int
    received_sample_count: int
    lost_sample_count: int
    clear_sample_count: int
    obstructed_sample_count: int
    building_count: int
    penetration_db: float
    loss_per_100m_db: float
    path_loss_exponent: float
    calibration_offset_db: float
    fitted_rmse_db: float


def _number(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _robust_fit(features: np.ndarray, targets: np.ndarray) -> np.ndarray:
    coefficients, *_ = np.linalg.lstsq(features, targets, rcond=None)
    for _ in range(30):
        residuals = targets - features @ coefficients
        center = float(np.median(residuals))
        scale = 1.4826 * float(np.median(np.abs(residuals - center)))
        if scale < 1e-6:
            break
        cutoff = 1.345 * scale
        weights = np.ones_like(residuals)
        outside = np.abs(residuals) > cutoff
        weights[outside] = cutoff / np.abs(residuals[outside])
        root_weights = np.sqrt(weights)
        updated, *_ = np.linalg.lstsq(
            features * root_weights[:, None], targets * root_weights, rcond=None
        )
        if float(np.max(np.abs(updated - coefficients))) < 1e-7:
            coefficients = updated
            break
        coefficients = updated
    return coefficients


def _censored_link_fit(
    features: np.ndarray,
    budgets: np.ndarray,
    observed_rssi: np.ndarray,
    received: np.ndarray,
    receive_floor_dbm: float,
    sigma_db: float = 8.0,
) -> np.ndarray:
    """Fit model parameters while treating failed packets as below the RF floor.

    Successful packets contribute their measured RSSI. Failed probes contribute
    the probability that their unobserved RSSI fell below ``receive_floor_dbm``.
    Keeping the intercept fixed at zero makes the result directly usable by the
    simulator instead of hiding link loss in an unapplied calibration offset.
    """
    # Columns map directly to path exponent, building penetration, and inside loss.
    scales = np.asarray([1.0, 0.2, 0.5], dtype=float)
    scaled_features = features * scales
    parameters = np.asarray([3.3, 3.0, 3.0], dtype=float) / scales
    first_moment = np.zeros(3, dtype=float)
    second_moment = np.zeros(3, dtype=float)
    sigma_db = max(2.0, float(sigma_db))
    lost = ~received
    for iteration in range(1, 12_001):
        predicted_rssi = budgets + scaled_features @ parameters
        gradient_rssi = np.zeros(len(predicted_rssi), dtype=float)
        gradient_rssi[received] = (
            predicted_rssi[received] - observed_rssi[received]
        ) / (sigma_db * sigma_db)
        z_scores = np.clip(
            (receive_floor_dbm - predicted_rssi[lost]) / sigma_db,
            -8.0,
            8.0,
        )
        density = np.exp(-0.5 * np.square(z_scores)) / math.sqrt(2.0 * math.pi)
        distribution = np.asarray(
            [0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0))) for z in z_scores],
            dtype=float,
        )
        gradient_rssi[lost] = density / (sigma_db * np.maximum(distribution, 1e-12))
        gradient = scaled_features.T @ gradient_rssi / len(predicted_rssi)
        first_moment = 0.9 * first_moment + 0.1 * gradient
        second_moment = 0.999 * second_moment + 0.001 * np.square(gradient)
        parameters -= 0.02 * first_moment / (np.sqrt(second_moment) + 1e-8)
        raw = parameters * scales
        raw[0] = np.clip(raw[0], 1.5, 6.0)
        raw[1:] = np.clip(raw[1:], 0.0, 30.0)
        parameters = raw / scales
        if iteration > 1000 and float(np.max(np.abs(gradient))) < 1e-7:
            break
    return parameters * scales


def _building_exposure(
    model: PropagationModel,
    source: Node,
    target: Node,
) -> tuple[float, float]:
    fixed_exposure = 0.0
    distance_exposure = 0.0
    planar_distance = max(1.0, math.hypot(target.x - source.x, target.y - source.y))
    wavelength = model.SPEED_OF_LIGHT / (source.radio.frequency_mhz * 1_000_000.0)
    for obstacle in model._candidate_obstacles(source, target):
        if not obstacle.enabled or obstacle.kind != "Building":
            continue
        inside_length, midpoint_t, _exit_t = model._obstacle_intersection(obstacle, source, target)
        if midpoint_t is None:
            continue
        los_z = source.antenna_z + (target.antenna_z - source.antenna_z) * midpoint_t
        top_z = obstacle.base_elevation_m + obstacle.height_m
        height_factor = 1.0
        if los_z > top_z:
            d1 = planar_distance * midpoint_t
            d2 = planar_distance - d1
            fresnel = math.sqrt(max(0.0, wavelength * d1 * d2 / planar_distance))
            clearance = los_z - top_z
            if clearance >= 0.6 * fresnel:
                continue
            height_factor = max(0.1, 1.0 - clearance / max(0.1, 0.6 * fresnel))
        fixed_exposure += height_factor
        distance_exposure += (inside_length / 100.0) * height_factor
    return fixed_exposure, distance_exposure


def fit_building_calibration(
    scenario: Scenario,
    measurements: Iterable[dict[str, object]],
) -> BuildingCalibration:
    """Fit global per-building loss while retaining the scenario RF baseline."""
    rows = list(measurements)
    buildings = [obstacle for obstacle in scenario.obstacles if obstacle.kind == "Building" and obstacle.enabled]
    if not buildings:
        raise SurveyCalibrationError("The current scenario has no enabled buildings to calibrate.")

    base_points = [
        (_number(row.get("base_latitude")), _number(row.get("base_longitude")))
        for row in rows
        if _truthy(row.get("base_gps_lock"))
    ]
    base_points = [(lat, lon) for lat, lon in base_points if lat is not None and lon is not None]
    if not base_points:
        raise SurveyCalibrationError("The loaded survey has no valid base GPS position.")
    base_x, base_y = latlon_to_world(
        median(point[0] for point in base_points),
        median(point[1] for point in base_points),
        scenario.environment.map_center_lat,
        scenario.environment.map_center_lon,
    )

    base_ground = scenario.environment.ground_elevation(base_x, base_y) or 0.0
    base = Node(x=base_x, y=base_y, elevation_m=base_ground, antenna_height_m=1.5)
    mobile = Node(antenna_height_m=1.5)
    model = PropagationModel(scenario)
    features: list[list[float]] = []
    budgets: list[float] = []
    observed: list[float] = []
    received: list[bool] = []
    clear_samples = 0
    obstructed_samples = 0

    for row in rows:
        if not _truthy(row.get("mobile_gps_lock")):
            continue
        latitude = _number(row.get("mobile_latitude"))
        longitude = _number(row.get("mobile_longitude"))
        observed_rssi = _number(row.get("forward_rssi_dbm"))
        was_received = _truthy(row.get("forward_received"))
        valid_rssi = observed_rssi is not None and -140.0 <= observed_rssi <= -10.0
        if latitude is None or longitude is None or (was_received and not valid_rssi):
            continue
        mobile.x, mobile.y = latlon_to_world(
            latitude,
            longitude,
            scenario.environment.map_center_lat,
            scenario.environment.map_center_lon,
        )
        mobile.elevation_m = scenario.environment.ground_elevation(mobile.x, mobile.y) or 0.0
        frequency_hz = _number(row.get("frequency_hz")) or 906_875_000.0
        tx_power_dbm = _number(row.get("tx_power_dbm"))
        mobile.radio.frequency_mhz = frequency_hz / 1_000_000.0
        mobile.tx_power_dbm = tx_power_dbm if tx_power_dbm is not None else 22.0
        base.radio.frequency_mhz = mobile.radio.frequency_mhz
        distance = max(1.0, math.hypot(base.x - mobile.x, base.y - mobile.y))
        fspl_1m = 20.0 * math.log10(
            4.0 * math.pi * frequency_hz / PropagationModel.SPEED_OF_LIGHT
        )
        received_budget = (
            mobile.tx_power_dbm
            + mobile.antenna_gain_dbi
            + base.antenna_gain_dbi
            - mobile.cable_loss_db
            - base.cable_loss_db
            - scenario.environment.weather_loss_db
        )
        fixed_exposure, distance_exposure = _building_exposure(model, mobile, base)
        if fixed_exposure > 0.0:
            obstructed_samples += 1
        else:
            clear_samples += 1
        features.append([-10.0 * math.log10(distance), -fixed_exposure, -distance_exposure])
        budgets.append(received_budget - fspl_1m)
        observed.append(observed_rssi if valid_rssi and observed_rssi is not None else 0.0)
        received.append(was_received)

    received_count = sum(received)
    lost_count = len(received) - received_count
    if received_count < 20 or lost_count < 10:
        raise SurveyCalibrationError(
            "Calibration requires at least 20 received RSSI samples and 10 failed probes."
        )
    if clear_samples < 5 or obstructed_samples < 5:
        raise SurveyCalibrationError(
            "Calibration requires at least five clear-path and five building-obstructed receptions."
        )

    feature_array = np.asarray(features, dtype=float)
    budget_array = np.asarray(budgets, dtype=float)
    observed_array = np.asarray(observed, dtype=float)
    received_array = np.asarray(received, dtype=bool)
    if np.linalg.matrix_rank(feature_array) < feature_array.shape[1]:
        raise SurveyCalibrationError("The survey does not contain enough varied building crossings to separate the losses.")
    fitted_exponent = scenario.environment.path_loss_exponent
    distance_loss = float(OBSTACLE_DEFAULTS["Building"][3])
    receive_floor = PropagationModel.sensitivity(base) + MIN_DECODE_MARGIN_DB
    best: tuple[float, float, float, float] | None = None
    for candidate in np.arange(0.0, 30.001, 0.05):
        parameters = np.asarray([fitted_exponent, candidate, distance_loss])
        candidate_rssi = budget_array + feature_array @ parameters
        predicted_received = candidate_rssi >= receive_floor
        accuracy = float(np.mean(predicted_received == received_array))
        count_error = abs(int(np.sum(predicted_received)) - received_count)
        received_rmse = math.sqrt(float(np.mean(np.square(
            observed_array[received_array] - candidate_rssi[received_array]
        ))))
        score = (accuracy, -float(count_error), -received_rmse, -float(candidate))
        if best is None or score > best:
            best = score
            penetration = float(candidate)
    penetration = round(penetration, 2)
    fitted_rssi = budget_array + feature_array @ np.asarray(
        [fitted_exponent, penetration, distance_loss]
    )
    rmse = math.sqrt(float(np.mean(np.square(
        observed_array[received_array] - fitted_rssi[received_array]
    ))))
    return BuildingCalibration(
        sample_count=len(features),
        received_sample_count=received_count,
        lost_sample_count=lost_count,
        clear_sample_count=clear_samples,
        obstructed_sample_count=obstructed_samples,
        building_count=len(buildings),
        penetration_db=penetration,
        loss_per_100m_db=distance_loss,
        path_loss_exponent=fitted_exponent,
        calibration_offset_db=0.0,
        fitted_rmse_db=rmse,
    )


def apply_building_calibration(scenario: Scenario, calibration: BuildingCalibration) -> int:
    """Apply measured values to every building and remove arbitrary range limits."""
    changed = 0
    for obstacle in scenario.obstacles:
        if obstacle.kind != "Building":
            continue
        obstacle.attenuation_db = round(calibration.penetration_db, 2)
        obstacle.loss_per_100m_db = round(calibration.loss_per_100m_db, 2)
        obstacle.behavior = "ATTENUATE"
        obstacle.max_range_beyond_m = 0.0
        changed += 1
    return changed
