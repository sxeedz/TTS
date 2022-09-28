import numpy as np
import torch
from scipy import signal as sig
from torch import nn
from torch.nn import functional as F
from torch.nn.modules.conv import Conv1d
from torch.nn.utils import spectral_norm, weight_norm

from TTS.vocoder.models.hifigan_discriminator import DiscriminatorP


class DiscriminatorS(torch.nn.Module):
    """HiFiGAN Scale Discriminator. Channel sizes are different from the original HiFiGAN.

    Args:
        use_spectral_norm (bool): if `True` swith to spectral norm instead of weight norm.
    """

    def __init__(self, use_spectral_norm=False):
        super().__init__()
        norm_f = nn.utils.spectral_norm if use_spectral_norm else nn.utils.weight_norm
        self.convs = nn.ModuleList(
            [
                norm_f(Conv1d(1, 16, 15, 1, padding=7)),
                norm_f(Conv1d(16, 64, 41, 4, groups=4, padding=20)),
                norm_f(Conv1d(64, 256, 41, 4, groups=16, padding=20)),
                norm_f(Conv1d(256, 1024, 41, 4, groups=64, padding=20)),
                norm_f(Conv1d(1024, 1024, 41, 4, groups=256, padding=20)),
                norm_f(Conv1d(1024, 1024, 5, 1, padding=2)),
            ]
        )
        self.conv_post = norm_f(Conv1d(1024, 1, 3, 1, padding=1))

    def forward(self, x):
        """
        Args:
            x (Tensor): input waveform.

        Returns:
            Tensor: discriminator scores.
            List[Tensor]: list of features from the convolutiona layers.
        """
        feat = []
        for l in self.convs:
            x = l(x)
            x = torch.nn.functional.leaky_relu(x, 0.1)
            feat.append(x)
        x = self.conv_post(x)
        feat.append(x)
        x = torch.flatten(x, 1, -1)
        return x, feat


class CoMBD(torch.nn.Module):
    def __init__(self, filters, kernels, groups, strides, use_spectral_norm=False):
        super(CoMBD, self).__init__()
        norm_f = weight_norm if use_spectral_norm == False else spectral_norm
        self.convs = nn.ModuleList()
        init_channel = 1
        for i, (f, k, g, s) in enumerate(zip(filters, kernels, groups, strides)):
            self.convs.append(norm_f(Conv1d(init_channel, f, k, s, padding=get_padding(k, 1), groups=g)))
            init_channel = f
        self.conv_post = norm_f(Conv1d(filters[-1], 1, 3, 1, padding=get_padding(3, 1)))

    def forward(self, x):
        fmap = []
        for l in self.convs:
            x = l(x)
            x = F.leaky_relu(x, 0.1)
            fmap.append(x)
        x = self.conv_post(x)
        # fmap.append(x)
        x = torch.flatten(x, 1, -1)
        return x, fmap


class MultiCoMBDiscriminator(torch.nn.Module):
    def __init__(self, kernels, channels, groups, strides):
        super(MultiCoMBDiscriminator, self).__init__()
        self.combd_1 = CoMBD(filters=channels, kernels=kernels[0], groups=groups, strides=strides)
        self.combd_2 = CoMBD(filters=channels, kernels=kernels[1], groups=groups, strides=strides)
        self.combd_3 = CoMBD(filters=channels, kernels=kernels[2], groups=groups, strides=strides)

        self.pqmf_2 = PQMF(N=2, taps=256, cutoff=0.25, beta=10.0)
        self.pqmf_4 = PQMF(N=8, taps=192, cutoff=0.13, beta=10.0)

    def forward(self, x, x_hat, x2_hat, x1_hat):
        y = []
        y_hat = []
        fmap = []
        fmap_hat = []

        p3, p3_fmap = self.combd_3(x)
        y.append(p3)
        fmap.append(p3_fmap)

        p3_hat, p3_fmap_hat = self.combd_3(x_hat)
        y_hat.append(p3_hat)
        fmap_hat.append(p3_fmap_hat)

        x2_ = self.pqmf_2(x)[:, :1, :]  # Select first band
        x1_ = self.pqmf_4(x)[:, :1, :]  # Select first band

        x2_hat_ = self.pqmf_2(x_hat)[:, :1, :]
        x1_hat_ = self.pqmf_4(x_hat)[:, :1, :]

        p2_, p2_fmap_ = self.combd_2(x2_)
        y.append(p2_)
        fmap.append(p2_fmap_)

        p2_hat_, p2_fmap_hat_ = self.combd_2(x2_hat)
        y_hat.append(p2_hat_)
        fmap_hat.append(p2_fmap_hat_)

        p1_, p1_fmap_ = self.combd_1(x1_)
        y.append(p1_)
        fmap.append(p1_fmap_)

        p1_hat_, p1_fmap_hat_ = self.combd_1(x1_hat)
        y_hat.append(p1_hat_)
        fmap_hat.append(p1_fmap_hat_)

        p2, p2_fmap = self.combd_2(x2_)
        y.append(p2)
        fmap.append(p2_fmap)

        p2_hat, p2_fmap_hat = self.combd_2(x2_hat_)
        y_hat.append(p2_hat)
        fmap_hat.append(p2_fmap_hat)

        p1, p1_fmap = self.combd_1(x1_)
        y.append(p1)
        fmap.append(p1_fmap)

        p1_hat, p1_fmap_hat = self.combd_1(x1_hat_)
        y_hat.append(p1_hat)
        fmap_hat.append(p1_fmap_hat)

        return y, y_hat, fmap, fmap_hat


def get_padding(kernel_size, dilation=1):
    return int((kernel_size * dilation - dilation) / 2)


class MDC(torch.nn.Module):
    def __init__(self, in_channel, channel, kernel, stride, dilations, use_spectral_norm=False):
        super(MDC, self).__init__()
        norm_f = weight_norm if use_spectral_norm == False else spectral_norm
        self.convs = torch.nn.ModuleList()
        self.num_dilations = len(dilations)
        for d in dilations:
            self.convs.append(
                norm_f(Conv1d(in_channel, channel, kernel, stride=1, padding=get_padding(kernel, d), dilation=d))
            )

        self.conv_out = norm_f(Conv1d(channel, channel, 3, stride=stride, padding=get_padding(3, 1)))

    def forward(self, x):
        xs = None
        for l in self.convs:
            if xs is None:
                xs = l(x)
            else:
                xs += l(x)

        x = xs / self.num_dilations

        x = self.conv_out(x)
        x = F.leaky_relu(x, 0.1)
        return x


class SubBandDiscriminator(torch.nn.Module):
    def __init__(self, init_channel, channels, kernel, strides, dilations, use_spectral_norm=False):
        super(SubBandDiscriminator, self).__init__()
        norm_f = weight_norm if use_spectral_norm == False else spectral_norm

        self.mdcs = torch.nn.ModuleList()

        for c, s, d in zip(channels, strides, dilations):
            self.mdcs.append(MDC(init_channel, c, kernel, s, d))
            init_channel = c
        self.conv_post = norm_f(Conv1d(init_channel, 1, 3, padding=get_padding(3, 1)))

    def forward(self, x):
        fmap = []

        for l in self.mdcs:
            x = l(x)
            fmap.append(x)
        x = self.conv_post(x)
        # fmap.append(x)
        x = torch.flatten(x, 1, -1)

        return x, fmap


class PQMF(torch.nn.Module):
    def __init__(self, N=4, taps=62, cutoff=0.15, beta=9.0):
        super(PQMF, self).__init__()

        self.N = N
        self.taps = taps
        self.cutoff = cutoff
        self.beta = beta

        QMF = sig.firwin(taps + 1, cutoff, window=("kaiser", beta))
        H = np.zeros((N, len(QMF)))
        G = np.zeros((N, len(QMF)))
        for k in range(N):
            constant_factor = (
                (2 * k + 1) * (np.pi / (2 * N)) * (np.arange(taps + 1) - ((taps - 1) / 2))
            )  # TODO: (taps - 1) -> taps
            phase = (-1) ** k * np.pi / 4
            H[k] = 2 * QMF * np.cos(constant_factor + phase)

            G[k] = 2 * QMF * np.cos(constant_factor - phase)

        H = torch.from_numpy(H[:, None, :]).float()
        G = torch.from_numpy(G[None, :, :]).float()

        self.register_buffer("H", H)
        self.register_buffer("G", G)

        updown_filter = torch.zeros((N, N, N)).float()
        for k in range(N):
            updown_filter[k, k, 0] = 1.0
        self.register_buffer("updown_filter", updown_filter)
        self.N = N

        self.pad_fn = torch.nn.ConstantPad1d(taps // 2, 0.0)

    def forward(self, x):
        return self.analysis(x)

    def analysis(self, x):
        return F.conv1d(x, self.H, padding=self.taps // 2, stride=self.N)

    def synthesis(self, x):
        x = F.conv_transpose1d(x, self.updown_filter * self.N, stride=self.N)
        x = F.conv1d(x, self.G, padding=self.taps // 2)
        return x


class MultiSubBandDiscriminator(torch.nn.Module):
    def __init__(
        self,
        tkernels,
        fkernel,
        tchannels,
        fchannels,
        tstrides,
        fstride,
        tdilations,
        fdilations,
        tsubband,
        n,
        m,
        freq_init_ch,
    ):

        super(MultiSubBandDiscriminator, self).__init__()

        self.fsbd = SubBandDiscriminator(
            init_channel=freq_init_ch, channels=fchannels, kernel=fkernel, strides=fstride, dilations=fdilations
        )

        self.tsubband1 = tsubband[0]
        self.tsbd1 = SubBandDiscriminator(
            init_channel=self.tsubband1,
            channels=tchannels,
            kernel=tkernels[0],
            strides=tstrides[0],
            dilations=tdilations[0],
        )

        self.tsubband2 = tsubband[1]
        self.tsbd2 = SubBandDiscriminator(
            init_channel=self.tsubband2,
            channels=tchannels,
            kernel=tkernels[1],
            strides=tstrides[1],
            dilations=tdilations[1],
        )

        self.tsubband3 = tsubband[2]
        self.tsbd3 = SubBandDiscriminator(
            init_channel=self.tsubband3,
            channels=tchannels,
            kernel=tkernels[2],
            strides=tstrides[2],
            dilations=tdilations[2],
        )

        self.pqmf_n = PQMF(N=n, taps=256, cutoff=0.03, beta=10.0)
        self.pqmf_m = PQMF(N=m, taps=256, cutoff=0.1, beta=9.0)

    def forward(self, x, x_hat):
        fmap = []
        fmap_hat = []
        y = []
        y_hat = []

        # Time analysis
        xn = self.pqmf_n(x)
        xn_hat = self.pqmf_n(x_hat)

        q3, feat_q3 = self.tsbd3(xn[:, : self.tsubband3, :])
        q3_hat, feat_q3_hat = self.tsbd3(xn_hat[:, : self.tsubband3, :])
        y.append(q3)
        y_hat.append(q3_hat)
        fmap.append(feat_q3)
        fmap_hat.append(feat_q3_hat)

        q2, feat_q2 = self.tsbd2(xn[:, : self.tsubband2, :])
        q2_hat, feat_q2_hat = self.tsbd2(xn_hat[:, : self.tsubband2, :])
        y.append(q2)
        y_hat.append(q2_hat)
        fmap.append(feat_q2)
        fmap_hat.append(feat_q2_hat)

        q1, feat_q1 = self.tsbd1(xn[:, : self.tsubband1, :])
        q1_hat, feat_q1_hat = self.tsbd1(xn_hat[:, : self.tsubband1, :])
        y.append(q1)
        y_hat.append(q1_hat)
        fmap.append(feat_q1)
        fmap_hat.append(feat_q1_hat)

        # Frequency analysis
        xm = self.pqmf_m(x)
        xm_hat = self.pqmf_m(x_hat)

        xm = xm.transpose(-2, -1)
        xm_hat = xm_hat.transpose(-2, -1)

        q4, feat_q4 = self.fsbd(xm)
        q4_hat, feat_q4_hat = self.fsbd(xm_hat)
        y.append(q4)
        y_hat.append(q4_hat)
        fmap.append(feat_q4)
        fmap_hat.append(feat_q4_hat)

        return y, y_hat, fmap, fmap_hat


class VitsDiscriminator(nn.Module):
    """VITS discriminator wrapping one Scale Discriminator and a stack of Period Discriminator.

    ::
        waveform -> ScaleDiscriminator() -> scores_sd, feats_sd --> append() -> scores, feats
               |--> MultiPeriodDiscriminator() -> scores_mpd, feats_mpd ^

    Args:
        use_spectral_norm (bool): if `True` swith to spectral norm instead of weight norm.
    """

    def __init__(self, args):
        super().__init__()
        self.mcmbd = MultiCoMBDiscriminator(
            args.combd_kernels, args.combd_channels, args.combd_groups, args.combd_strides
        )
        self.msbd = MultiSubBandDiscriminator(
            tkernels=args.tkernels,
            fkernel=args.fkernel,
            tchannels=args.tchannels,
            fchannels=args.fchannels,
            tstrides=args.tstrides,
            fstride=args.fstride,
            tdilations=args.tdilations,
            fdilations=args.fdilations,
            tsubband=args.tsubband,
            n=args.pqmf_n,
            m=args.pqmf_m,
            freq_init_ch=args.freq_init_ch,
        )

    def forward(self, x, x_hat, x1_hat, x2_hat):
        """
        Args:
            x (Tensor): ground truth waveform.
            x_hat (Tensor): predicted waveform.

        Returns:
            List[Tensor]: discriminator scores.
            List[List[Tensor]]: list of list of features from each layers of each discriminator.
        """
        x_scores = []
        x_hat_scores = [] if x_hat is not None else None
        x_feats = []
        x_hat_feats = [] if x_hat is not None else None

        x_scores, x_hat_scores, x_feats, x_hat_feats = self.mcmbd(x, x_hat, x2_hat, x1_hat)
        x_scores2, x_hat_scores2, x_feats2, x_hat_feats2 = self.msbd(x, x_hat)


        x_scores = x_scores + x_scores2
        x_hat_scores = x_hat_scores + x_hat_scores2

        x_feats = x_feats + x_feats2
        x_hat_feats = x_hat_feats + x_hat_feats2

        return x_scores, x_hat_scores, x_feats, x_hat_feats
