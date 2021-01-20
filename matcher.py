import os
import sys
import warnings

import faiss
import julius
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.multiprocessing as mp
import tensorboardX
import tqdm

# torchaudio currently (0.7) will throw warning that cannot be disabled
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import torchaudio

import simpleutils
from model import FpNetwork
from datautil.audio import stream_audio

class MusicDataset(torch.utils.data.Dataset):
    def __init__(self, file_list, params):
        self.params = params
        self.sample_rate = self.params['sample_rate']
        self.segment_size = int(self.sample_rate * self.params['segment_size'])
        self.hop_size = int(self.sample_rate * self.params['hop_size'])
        with open(file_list, 'r', encoding='utf8') as fin:
            self.files = []
            for x in fin:
                if x.endswith('\n'):
                    x = x[:-1]
                self.files.append(x)
    
    def __getitem__(self, index):
        smprate = self.sample_rate
        
        # resample
        stm = stream_audio(self.files[index])
        resampler = julius.ResampleFrac(stm.sample_rate, smprate)
        arr = []
        n = 0
        total = 0
        minute = stm.sample_rate * 60
        second = stm.sample_rate
        new_min = smprate * 60
        new_sec = smprate
        strip_head = 0
        wav = []
        for b in stm.stream:
            b = np.array(b).reshape([-1, stm.nchannels])
            b = np.mean(b, axis=1, dtype=np.float32) * (1/32768)
            arr.append(b)
            n += b.shape[0]
            total += b.shape[0]
            if n >= minute:
                arr = np.concatenate(arr)
                b = arr[:minute]
                out = torch.from_numpy(b)
                wav.append(resampler(out)[strip_head : new_min-new_sec//2])
                arr = [arr[minute-second:].copy()]
                strip_head = new_sec//2
                n -= minute-second
        # resample tail part
        arr = np.concatenate(arr)
        out = torch.from_numpy(arr)
        wav.append(resampler(out)[strip_head : ])
        wav = torch.cat(wav)

        if wav.shape[0] < self.segment_size:
            # this "music" is too short and need to be extended
            wav = F.pad(wav, (0, self.segment_size - wav.shape[0]))
        
        # normalize volume
        wav = wav.unfold(0, self.segment_size, self.hop_size)
        wav = wav - wav.mean(dim=1).unsqueeze(1)
        wav = F.normalize(wav, p=2, dim=1)
        
        return index, self.files[index], wav
    
    def __len__(self):
        return len(self.files)

if __name__ == "__main__":
    mp.set_start_method('spawn')
    torch.set_num_threads(1)
    file_list_for_query = sys.argv[1]
    dir_for_db = sys.argv[2]
    result_file = sys.argv[3]
    configs = 'configs/default.json'
    if len(sys.argv) >= 5:
        configs = sys.argv[4]
    params = simpleutils.read_config(configs)

    d = params['model']['d']
    h = params['model']['h']
    u = params['model']['u']
    F_bin = params['n_mels']
    segn = int(params['segment_size'] * params['sample_rate'])
    T = (segn + params['stft_hop'] - 1) // params['stft_hop']
    
    topk = 10

    print('loading model...')
    device = torch.device('cuda')
    model = FpNetwork(d, h, u, F_bin, T).to(device)
    model.load_state_dict(torch.load(os.path.join(params['model_dir'], 'model.pt')))
    print('model loaded')
    
    print('loading database...')
    landmarkKey = np.fromfile(os.path.join(dir_for_db, 'landmarkKey'), dtype=np.int64)
    index = faiss.read_index(os.path.join(dir_for_db, 'landmarkValue'))
    print('database loaded')

    # doing inference, turn off gradient
    model.eval()
    for param in model.parameters():
        param.requires_grad = False

    dataset = MusicDataset(file_list_for_query, params)
    # no task parallelism
    loader = DataLoader(dataset, num_workers=0)
    
    mel = torchaudio.transforms.MelSpectrogram(
        sample_rate=params['sample_rate'],
        n_fft=params['stft_n'],
        hop_length=params['stft_hop'],
        f_min=params['f_min'],
        f_max=params['f_max'],
        n_mels=params['n_mels'],
        window_fn=torch.hann_window).to(device)
    
    fout = open(result_file, 'w')
    for dat in tqdm.tqdm(loader):
        embeddings = []
        i, name, wav = dat
        i = int(i) # i is leaking file handles!
        # batch size should be less than 20 because query contains at most 19 segments
        for batch in DataLoader(wav.squeeze(0), batch_size=16):
            g = batch.to(device)
            
            # Mel spectrogram
            with warnings.catch_warnings():
                # torchaudio is still using deprecated function torch.rfft
                warnings.simplefilter("ignore")
                g = mel(g)
            g = torch.log(g + 1e-8)
            z = model(g).cpu()
            embeddings.append(z)
        embeddings = torch.cat(embeddings)
        dists, ids = index.search(x=embeddings.numpy(), k=topk)
        print(ids.shape)
        scoreboard = {}
        for t in range(ids.shape[0]):
            for j in range(topk):
                t0 = int(ids[t, j] - t)
                if t0 in scoreboard:
                    scoreboard[t0] += float(dists[t, j])
                else:
                    scoreboard[t0] = float(dists[t, j])
        scoreboard = [(dist,id_) for id_,dist in scoreboard.items()]
        scoreboard.sort()
        print(scoreboard[-1])
    fout.close()
