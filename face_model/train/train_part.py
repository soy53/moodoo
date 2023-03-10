import torch
from helper.nerf_helpers import get_minibatches
from helper.nerf_helpers import volume_render_radiance_field, ndc_rays

sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
from helper import nerf_helpers


# models/nerf/train_utils.py가 원본임
def run_network(network_fn, pts, ray_batch, chunksize, embed_fn, embeddirs_fn, expressions = None, latent_code = None):

    pts_flat = pts.reshape((-1, pts.shape[-1]))
    embedded = embed_fn(pts_flat)
    if embeddirs_fn is not None:
        viewdirs = ray_batch[..., None, -3:]
        input_dirs = viewdirs.expand(pts.shape)
        input_dirs_flat = input_dirs.reshape((-1, input_dirs.shape[-1]))
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat((embedded, embedded_dirs), dim=-1)

    batches = get_minibatches(embedded, chunksize=chunksize)
    if expressions is None:
        preds = [network_fn(batch) for batch in batches]
    elif latent_code is not None:
        preds = [network_fn(batch, expressions, latent_code) for batch in batches]
    else:
        preds = [network_fn(batch, expressions) for batch in batches]
    radiance_field = torch.cat(preds, dim=0)
    radiance_field = radiance_field.reshape(
        list(pts.shape[:-1]) + [radiance_field.shape[-1]]
    )

    del embedded, input_dirs_flat
    return radiance_field


def predict_and_render_radiance(
    ray_batch,
    model_coarse,
    model_fine,
    options,
    mode="train",
    encode_position_fn=None,
    encode_direction_fn=None,
    expressions = None,
    background_prior = None,
    latent_code = None,
    ray_dirs_fake = None
):
    # TESTED
    num_rays = ray_batch.shape[0]
    ro, rd = ray_batch[..., :3], ray_batch[..., 3:6].clone() # TODO remove clone ablation rays
    bounds = ray_batch[..., 6:8].view((-1, 1, 2))
    near, far = bounds[..., 0], bounds[..., 1]
    # TODO: Use actual values for "near" and "far" (instead of 0. and 1.)
    # when not enabling "ndc".
    t_vals = torch.linspace(
        0.0,
        1.0,
        getattr(options.nerf, mode).num_coarse,
        dtype=ro.dtype,
        device=ro.device,
    )
    if not getattr(options.nerf, mode).lindisp:
        z_vals = near * (1.0 - t_vals) + far * t_vals
    else:
        z_vals = 1.0 / (1.0 / near * (1.0 - t_vals) + 1.0 / far * t_vals)
    z_vals = z_vals.expand([num_rays, getattr(options.nerf, mode).num_coarse])

    if getattr(options.nerf, mode).perturb:
        # Get intervals between samples.
        mids = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        upper = torch.cat((mids, z_vals[..., -1:]), dim=-1)
        lower = torch.cat((z_vals[..., :1], mids), dim=-1)
        # Stratified samples in those intervals.
        t_rand = torch.rand(z_vals.shape, dtype=ro.dtype, device=ro.device)
        z_vals = lower + (upper - lower) * t_rand
    # pts -> (num_rays, N_samples, 3)
    pts = ro[..., None, :] + rd[..., None, :] * z_vals[..., :, None]
    # Uncomment to dump a ply file visualizing camera rays and sampling points
    #dump_rays(ro.detach().cpu().numpy(), pts.detach().cpu().numpy())
    if ray_dirs_fake:
        ray_batch[...,3:6] = ray_dirs_fake[0][...,3:6]

    radiance_field = run_network(
        model_coarse,
        pts,
        ray_batch,
        getattr(options.nerf, mode).chunksize,
        encode_position_fn,
        encode_direction_fn,
        expressions,
        latent_code
    )
    # make last RGB values of each ray, the background
    if background_prior is not None:
        radiance_field[:,-1,:3] = background_prior

    (
        rgb_coarse,
        disp_coarse,
        acc_coarse,
        weights,
        depth_coarse,
    ) = volume_render_radiance_field(
        radiance_field,
        z_vals,
        rd,
        radiance_field_noise_std=getattr(options.nerf, mode).radiance_field_noise_std,
        white_background=getattr(options.nerf, mode).white_background,
        background_prior=background_prior
    )

    rgb_fine, disp_fine, acc_fine = None, None, None
    if getattr(options.nerf, mode).num_fine > 0:
        # rgb_map_0, disp_map_0, acc_map_0 = rgb_map, disp_map, acc_map

        z_vals_mid = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        z_samples = nerf_helpers.sample_pdf(
            z_vals_mid,
            weights[..., 1:-1],
            getattr(options.nerf, mode).num_fine,
            det=(getattr(options.nerf, mode).perturb == 0.0),
        )
        z_samples = z_samples.detach()

        z_vals, _ = torch.sort(torch.cat((z_vals, z_samples), dim=-1), dim=-1)
        # pts -> (N_rays, N_samples + N_importance, 3)
        pts = ro[..., None, :] + rd[..., None, :] * z_vals[..., :, None]

        radiance_field = run_network(
            model_fine,
            pts,
            ray_batch,
            getattr(options.nerf, mode).chunksize,
            encode_position_fn,
            encode_direction_fn,
            expressions,
            latent_code
        )
        # make last RGB values of each ray, the background
        if background_prior is not None:
            radiance_field[:, -1, :3] = background_prior

        # Uncomment to dump a ply file visualizing camera rays and sampling points
        #dump_rays(ro.detach().cpu().numpy(), pts.detach().cpu().numpy(), radiance_field)

        #dump_rays(ro.detach().cpu().numpy(), pts.detach().cpu().numpy(), torch.softmax(radiance_field[:,:,-1],1).detach().cpu().numpy())

        #rgb_fine, disp_fine, acc_fine, _, depth_fine = volume_render_radiance_field(
        rgb_fine, disp_fine, acc_fine, weights, depth_fine = volume_render_radiance_field( # added use of weights
            radiance_field,
            z_vals,
            rd,
            radiance_field_noise_std=getattr(
                options.nerf, mode
            ).radiance_field_noise_std,
            white_background=getattr(options.nerf, mode).white_background,
            background_prior=background_prior
        )

    #return rgb_coarse, disp_coarse, acc_coarse, rgb_fine, disp_fine, acc_fine, depth_fine #added depth fine
    return rgb_coarse, disp_coarse, acc_coarse, rgb_fine, disp_fine, acc_fine, weights[:,-1] #changed last return val to fine_weights


def run_one_iter_of_nerf(
    height,
    width,
    focal_length,
    model_coarse,
    model_fine,
    ray_origins,
    ray_directions,
    options,
    mode="train",
    encode_position_fn=None,
    encode_direction_fn=None,
    expressions = None,
    background_prior=None,
    latent_code = None,
    ray_directions_ablation = None
):
    is_rad = torch.is_tensor(ray_directions_ablation)
    viewdirs = None
    if options.nerf.use_viewdirs:
        # Provide ray directions as input
        viewdirs = ray_directions
        viewdirs = viewdirs / viewdirs.norm(p=2, dim=-1).unsqueeze(-1)
        viewdirs = viewdirs.view((-1, 3))
    # Cache shapes now, for later restoration.
    restore_shapes = [
        ray_directions.shape,
        ray_directions.shape[:-1],
        ray_directions.shape[:-1],
    ]
    if model_fine:
        restore_shapes += restore_shapes
        restore_shapes += [ray_directions.shape[:-1]] # to return fine depth map
    if options.dataset.no_ndc is False:
        #print("calling ndc")
        ro, rd = ndc_rays(height, width, focal_length, 1.0, ray_origins, ray_directions)
        ro = ro.view((-1, 3))
        rd = rd.view((-1, 3))
    else:
        #print("calling ndc")
        #"caling normal rays (not NDC)"
        ro = ray_origins.view((-1, 3))
        rd = ray_directions.view((-1, 3))
        if is_rad:
            rd_ablations = ray_directions_ablation.view((-1, 3))
    near = options.dataset.near * torch.ones_like(rd[..., :1])
    far = options.dataset.far * torch.ones_like(rd[..., :1])
    rays = torch.cat((ro, rd, near, far), dim=-1)
    if is_rad:
        rays_ablation = torch.cat((ro, rd_ablations, near, far), dim=-1)
    if options.nerf.use_viewdirs: # TODO uncomment
        rays = torch.cat((rays, viewdirs), dim=-1)
    #
    #viewdirs = None  # TODO remove this paragraph
    if options.nerf.use_viewdirs:
        # Provide ray directions as input
        if is_rad:
            viewdirs = ray_directions_ablation
            viewdirs = viewdirs / viewdirs.norm(p=2, dim=-1).unsqueeze(-1)
            viewdirs = viewdirs.view((-1, 3))


    if is_rad:
        batches_ablation = get_minibatches(rays_ablation, chunksize=getattr(options.nerf, mode).chunksize)
    batches = get_minibatches(rays, chunksize=getattr(options.nerf, mode).chunksize)
    assert(batches[0].shape == batches[0].shape)
    background_prior = get_minibatches(background_prior, chunksize=getattr(options.nerf, mode).chunksize) if\
        background_prior is not None else background_prior
    #print("predicting")
    if is_rad:
        pred = [
            predict_and_render_radiance(
                batch,
                model_coarse,
                model_fine,
                options,
                mode,
                encode_position_fn=encode_position_fn,
                encode_direction_fn=encode_direction_fn,
                expressions = expressions,
                background_prior = background_prior[i] if background_prior is not None else background_prior,
                latent_code = latent_code,
                ray_dirs_fake = batches_ablation
            )
            for i,batch in enumerate(batches)
        ]
    else:
        pred = [
            predict_and_render_radiance(
                batch,
                model_coarse,
                model_fine,
                options,
                mode,
                encode_position_fn=encode_position_fn,
                encode_direction_fn=encode_direction_fn,
                expressions = expressions,
                background_prior = background_prior[i] if background_prior is not None else background_prior,
                latent_code = latent_code,
                ray_dirs_fake = None
            )
            for i,batch in enumerate(batches)
        ]
    #print("predicted")

    synthesized_images = list(zip(*pred))
    synthesized_images = [
        torch.cat(image, dim=0) if image[0] is not None else (None)
        for image in synthesized_images
    ]
    if mode == "validation":
        synthesized_images = [
            image.view(shape) if image is not None else None
            for (image, shape) in zip(synthesized_images, restore_shapes)
        ]

        # Returns rgb_coarse, disp_coarse, acc_coarse, rgb_fine, disp_fine, acc_fine
        # (assuming both the coarse and fine networks are used).
        if model_fine:
            return tuple(synthesized_images)
        else:
            # If the fine network is not used, rgb_fine, disp_fine, acc_fine are
            # set to None.
            return tuple(synthesized_images + [None, None, None])

    return tuple(synthesized_images)