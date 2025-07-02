import asyncio
from typing import Any, Dict

import torch
from loguru import logger
from PIL import Image

from app.core.config import get_settings

settings = get_settings()


class MLModel:
    def __init__(self):
        self.pipe = None
        self.device = None
        self.torch_dtype = None

    async def load_model(self):
        """
        Asynchronously loads the machine learning model and tokenizer.
        This is a long-running, blocking operation, so it's run in a thread.
        """
        logger.info("Initializing model... This may take a few minutes.")
        # Run the blocking model loading code in a separate thread
        await asyncio.to_thread(self._load_model_sync)
        logger.info(f"Model initialized successfully on device '{self.device}'.")

    def _load_model_sync(self):
        """
        Synchronous part of the model loading.
        This method is executed in a separate thread to avoid blocking the asyncio event loop.
        """
        try:
            self.device = self._get_device()
            self.torch_dtype = (
                torch.bfloat16 if self.device == "cuda" else torch.float32
            )

            if self.device == "cpu":
                logger.warning(
                    "CUDA not available. Running on CPU, which will be extremely slow."
                )

            from dfloat11 import DFloat11Model
            from diffusers import FluxKontextPipeline

            self.pipe = FluxKontextPipeline.from_pretrained(
                "black-forest-labs/FLUX.1-Kontext-dev",
                torch_dtype=self.torch_dtype,
            )
            DFloat11Model.from_pretrained(
                "DFloat11/FLUX.1-Kontext-dev-DF11",
                device="cpu",
                bfloat16_model=self.pipe.transformer,
            )

            if self.device == "cuda":
                self.pipe.enable_model_cpu_offload()
            else:
                self.pipe.to(self.device)

        except ImportError as e:
            logger.critical(f"A required library is not installed: {e}")
            raise  # Re-raise to be caught by the lifespan manager
        except Exception as e:
            logger.critical(f"Could not initialize the model. Error: {e}")
            raise

    def _get_device(self) -> str:
        """Determines the appropriate device for PyTorch."""
        if torch.cuda.is_available():
            return "cuda"
        # Add MPS (Apple Silicon) support if needed in the future
        # if torch.backends.mps.is_available():
        #     return "mps"
        return "cpu"

    async def generate_image(self, params: Dict[str, Any]) -> Image.Image:
        """
        Asynchronously generates an image using the loaded model.
        The actual inference is run in a thread to avoid blocking.
        """
        log_params = {k: v for k, v in params.items() if k != "image"}
        logger.info(f"Generating image with params: {log_params}")
        try:
            # The pipeline call is blocking, so run it in a thread
            processed_image = await asyncio.to_thread(self._run_pipeline, params)
            return processed_image
        except torch.cuda.OutOfMemoryError:
            logger.error(
                "CUDA Out of Memory. Try a smaller image size or reduce batch size."
            )
            raise RuntimeError(
                "Processing failed due to insufficient GPU memory."
            ) from None
        except RuntimeError as e:
            logger.error(f"A runtime error occurred during processing: {e}")
            raise RuntimeError(
                "An unexpected runtime error occurred during image generation."
            ) from e
        except Exception as e:
            logger.exception(
                f"An unexpected error occurred during image generation: {e}"
            )
            raise RuntimeError("An unexpected server error occurred.") from e

    def _run_pipeline(self, params: Dict[str, Any]) -> Image.Image:
        """
        Synchronous wrapper for the diffusers pipeline call.
        """
        # The generator needs to be created with the correct device
        seed = params.pop("seed_value")
        params["generator"] = torch.Generator(device=self.device).manual_seed(seed)

        return self.pipe(**params).images[0]


# Singleton instance of the model service
ml_model = MLModel()


def get_ml_model() -> MLModel:
    return ml_model
