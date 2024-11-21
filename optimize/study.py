import argparse
import warnings
from functools import partial

import numpy as np
import optuna
import pandas as pd
import yaml
from redis import Redis
from redis.commands.json.path import Path

from optimize.eval import Eval
from optimize.models import StudyConfig
from optimize.retriever import DefaultQueryRetriever

warnings.filterwarnings("ignore")

METRICS = {
    "ret_k": [],
    "algorithm": [],
    "ef_construction": [],
    "ef_runtime": [],
    "m": [],
    "distance_metric": [],
    "vector_data_type": [],
    "model": [],
    "model_dim": [],
    "f1@k": [],
    "embedding_latency": [],
    "indexing_time": [],
    "avg_query_latency": [],
    "obj_val": [],
    "retriever": [],
}


def load_config(config_path: str) -> StudyConfig:
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    return StudyConfig(**config)


def update_metric_row(eval_obj):
    METRICS["ret_k"].append(eval_obj.settings.ret_k)
    METRICS["algorithm"].append(eval_obj.settings.index.algorithm)
    METRICS["ef_construction"].append(eval_obj.settings.index.ef_construction)
    METRICS["ef_runtime"].append(eval_obj.settings.index.ef_runtime)
    METRICS["m"].append(eval_obj.settings.index.m)
    METRICS["distance_metric"].append(eval_obj.settings.index.distance_metric)
    METRICS["vector_data_type"].append(eval_obj.settings.index.vector_data_type)
    METRICS["model"].append(eval_obj.settings.embedding.model)
    METRICS["model_dim"].append(eval_obj.settings.embedding.dim)
    METRICS["f1@k"].append(eval_obj.f1_at_k)
    METRICS["embedding_latency"].append(eval_obj.embedding_latency)
    METRICS["indexing_time"].append(eval_obj.total_indexing_time)
    METRICS["avg_query_latency"].append(eval_obj.avg_query_latency)
    METRICS["obj_val"].append(eval_obj.obj_val)
    METRICS["retriever"].append(str(eval_obj.retriever))


def cost_fn(metrics: list, weights: list):
    return np.dot(np.array(metrics), np.array(weights))


def norm_metric(value: float):
    return 1 / (1 + value)


def objective(trial, study_config, custom_retrievers):
    # we want to max the overall F1 score
    model_info = trial.suggest_categorical(
        "model_info",
        [m.model_dump() for m in study_config.embedding_models],
    )

    if custom_retrievers:
        retriever_name = trial.suggest_categorical(
            "retriever", list(custom_retrievers.keys())
        )
        obj = custom_retrievers[retriever_name]
        retriever = obj["retriever"]
        additional_schema_fields = custom_retrievers.get(
            "additional_schema_fields", None
        )
    else:
        retriever = DefaultQueryRetriever

    algorithm = trial.suggest_categorical("algorithm", study_config.algorithms)
    vec_dtype = trial.suggest_categorical("var_dtype", study_config.vector_data_types)

    ret_k = trial.suggest_int("ret_k", study_config.ret_k[0], study_config.ret_k[1])

    if algorithm == "hnsw":
        ef_runtime = trial.suggest_categorical("ef_runtime", study_config.ef_runtime)
        ef_construction = trial.suggest_categorical(
            "ef_construction", study_config.ef_construction
        )
        m = trial.suggest_categorical("m", study_config.m)

        print(
            f"\n\n Running for: \n model_str: {model_info['model']} \n ef_runtime: {ef_runtime} \n ef_construction: {ef_construction} \n m: {m} \n\n"
        )

        e = Eval(
            model_provider=model_info["provider"],
            model_str=model_info["model"],
            embedding_dim=model_info["dim"],
            raw_data_path=study_config.raw_data_path,
            labeled_data_path=study_config.labeled_data_path,
            input_data_type=study_config.input_data_type,
            vector_data_type=vec_dtype,
            algorithm=algorithm,
            ef_runtime=ef_runtime,
            ef_construction=ef_construction,
            m=m,
            ret_k=ret_k,  # maybe make a independent variable
            find_threshold=False,
            retriever=retriever,
            additional_schema_fields=additional_schema_fields,
        )
    else:
        print(
            f"\n\n Running for: \n model_str: {model_info['model']} \n algorithm: {algorithm}"
        )
        e = Eval(
            model_provider=model_info["provider"],
            model_str=model_info["model"],
            embedding_dim=model_info["dim"],
            raw_data_path=study_config.raw_data_path,
            labeled_data_path=study_config.labeled_data_path,
            input_data_type=study_config.input_data_type,
            vector_data_type=vec_dtype,
            algorithm=algorithm,
            ret_k=ret_k,
            retriever=retriever,
            additional_schema_fields=additional_schema_fields,
        )

    e.calc_metrics()

    norm_index_time = norm_metric(e.total_indexing_time)
    norm_latency = norm_metric(e.embedding_latency)

    metric_values = [e.f1_at_k, norm_index_time, norm_latency]

    e.obj_val = cost_fn(metric_values, study_config.weights)

    update_metric_row(e)

    return e.obj_val


def run_study(study_config: StudyConfig, custom_retrievers=None):

    study = optuna.create_study(
        study_name="test",
        direction="maximize",
        sampler=optuna.samplers.TPESampler(),
        pruner=optuna.pruners.MedianPruner(),
    )

    obj = partial(
        objective, study_config=study_config, custom_retrievers=custom_retrievers
    )

    study.optimize(obj, n_trials=study_config.n_trials, n_jobs=study_config.n_jobs)
    print(f"Completed Bayesian optimization... \n\n")

    best_trial = study.best_trial
    print(f"Best Configuration: {best_trial.number}: {best_trial.params}:\n\n")
    print(f"Best Score: {best_trial.values}\n\n")

    # save study metrics to DB
    client = Redis.from_url(study_config.redis_url)

    client.json().set(f"study:{study_config.study_id}", Path.root_path(), METRICS)


def run_study_cli():
    parser = argparse.ArgumentParser(
        description="Tune hyperparameters for Redis Vector Store given config file"
    )
    parser.add_argument("--config", type=str, help="Path to config file")
    args = parser.parse_args()
    study_config = load_config(args.config)
    run_study(study_config)
