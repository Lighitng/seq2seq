import argparse
import nltk

from mxnet.rnn import LSTMCell, SequentialRNNCell
from itertools import takewhile, dropwhile

from utils import array_to_text

from seq2seq_iterator import *

from attention_cell import AttentionEncoderCell, DotAttentionCell

parser = argparse.ArgumentParser(description="Train RNN on Penn Tree Bank",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--infer', default=False, action='store_true',
                    help='whether to do inference instead of training')
parser.add_argument('--model-prefix', type=str, default=None,
                    help='path to save/load model')
parser.add_argument('--load-epoch', type=int, default=0,
                    help='load from epoch')
parser.add_argument('--num-layers', type=int, default=2,
                    help='number of stacked RNN layers')
parser.add_argument('--num-hidden', type=int, default=200,
                    help='hidden layer size')
parser.add_argument('--num-embed', type=int, default=200,
                    help='embedding layer size')
parser.add_argument('--bidirectional', type=bool, default=False,
                    help='whether to use bidirectional layers')
parser.add_argument('--gpus', type=str,
                    help='list of gpus to run, e.g. 0 or 0,2,5. empty means using cpu. ' \
                         'Increase batch size when using multiple gpus for best performance.')
parser.add_argument('--kv-store', type=str, default='device',
                    help='key-value store type')
parser.add_argument('--num-epochs', type=int, default=25,
                    help='max num of epochs')
parser.add_argument('--lr', type=float, default=0.01,
                    help='initial learning rate')
parser.add_argument('--optimizer', type=str, default='sgd',
                    help='the optimizer type')
parser.add_argument('--mom', type=float, default=0.0,
                    help='momentum for sgd')
parser.add_argument('--wd', type=float, default=0.00001,
                    help='weight decay for sgd')
parser.add_argument('--batch-size', type=int, default=32,
                    help='the batch size.')
parser.add_argument('--disp-batches', type=int, default=50,
                    help='show progress for every n batches')
parser.add_argument('--max-grad-norm', type=float, default=5.0,
                    help='maximum gradient norm (larger values will be clipped')
# When training a deep, complex model, it's recommended to stack fused RNN cells (one
# layer per cell) together instead of one with all layers. The reason is that fused RNN
# cells doesn't set gradients to be ready until the computation for the entire layer is
# completed. Breaking a multi-layer fused RNN cell into several one-layer ones allows
# gradients to be processed ealier. This reduces communication overhead, especially with
# multiple GPUs.
parser.add_argument('--stack-rnn', default=False,
                    help='stack fused RNN cells to reduce communication overhead')
parser.add_argument('--dropout', type=float, default='0.0',
                    help='dropout probability (1.0 - keep probability)')
parser.add_argument('--use-cudnn-cells', action='store_true',
                    help='Use CUDNN LSTM (mx.rnn.FusedRNNCell) for training instead of in-graph LSTM cells (mx.rnn.LSTMCell)')

#buckets = [32]
# buckets = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

start_label = 1
invalid_label = 0

reserved_tokens={'<PAD>':0, '<UNK>':1, '<EOS>':2, '<GO>':3}

def print_inferred_shapes(node, arg_shapes, aux_shapes, out_shapes):
    args = node.list_arguments()
    aux_states = node.list_auxiliary_states()
    outputs = node.list_outputs()
    print("\n================================================")
    print("\nNODE: %s" % node.name)
    print("\n============")
    print("args:")
    print("============")
    if len(arg_shapes) == 0:
        print("N/A")
    for i in range(len(arg_shapes)):
        print("%s: %s" % (args[i], arg_shapes[i]))
    print("\n=============")
    print("aux_states:")
    print("=============")
    if len(aux_shapes) == 0:
        print("N/A")
    for i in range(len(aux_states)):
        print("%s: %s" % (aux_states[i], aux_shapes[i]))
    print("\n=============")
    print("outputs:")
    print("==============")
    if len(out_shapes) == 0:
        print("N/A")
    for i in range(len(outputs)):
        print("%s: %s" % (outputs[i], out_shapes[i]))
    print("\n================================================")
    print("\n")

def _normalize_sequence(length, inputs, layout, merge, in_layout=None):
    from mxnet import symbol

    assert inputs is not None, \
        "unroll(inputs=None) has been deprecated. " \
        "Please create input variables outside unroll."

    axis = layout.find('T')
    in_axis = in_layout.find('T') if in_layout is not None else axis
    if isinstance(inputs, symbol.Symbol):
        if merge is False:
            assert len(inputs.list_outputs()) == 1, \
                "unroll doesn't allow grouped symbol as input. Please convert " \
                "to list with list(inputs) first or let unroll handle splitting."
            inputs = list(symbol.split(inputs, axis=in_axis, num_outputs=length,
                                       squeeze_axis=1))
    else:
        assert length is None or len(inputs) == length
        if merge is True:
            inputs = [symbol.expand_dims(i, axis=axis) for i in inputs]
            inputs = symbol.Concat(*inputs, dim=axis)
            in_axis = axis

    if isinstance(inputs, symbol.Symbol) and axis != in_axis:
        inputs = symbol.swapaxes(inputs, dim0=axis, dim1=in_axis)

    return inputs, axis

def get_data(layout):

    start = time()

    print("\nUnpickling training iterator")

    with open('./data/train_iterator.pkl', 'rb') as f: # _en_de.pkl
        train_iter = pickle.load(f)

    train_iter.initialize()
    train_iter.batch_size = args.batch_size

    print("\nUnpickling validation iterator")

    with open('./data/valid_iterator.pkl', 'rb') as f: # _en_de.pkl
        valid_iter = pickle.load(f)

    valid_iter.initialize()
    valid_iter.batch_size = args.batch_size

    print("\nEncoded source language sentences:\n")
    for i in range(5):
        print(array_to_text(train_iter.src_sent[i], train_iter.inv_src_vocab))

    print("\nEncoded target language sentences:\n")
    for i in range(5):
        print(array_to_text(valid_iter.targ_sent[i], train_iter.inv_targ_vocab))

    duration = time() - start

    print("\nDataset deserialization time: %.2f seconds\n" % duration)

    return train_iter, valid_iter, train_iter.src_vocab, train_iter.targ_vocab

# WORK IN PROGRESS !!!
def decoder_unroll(decoder, target_embed, targ_vocab, unroll_length, go_symbol, begin_state=None, layout='TNC', merge_outputs=None):

        decoder.reset()

        if begin_state is None:
            begin_state = decoder.begin_state()

        inputs, _ = _normalize_sequence(unroll_length, target_embed, layout, False)

        # Need to use hidden state from attention model, but <GO> as input
        states = begin_state
        outputs = []

        embed = inputs[0]

        # NEW 1
#        fc_weight = mx.sym.Variable('fc_weight')
#        fc_bias = mx.sym.Variable('fc_bias')
#        em_weight = mx.sym.Variable('em_weight')
#        for i in range(0, unroll_length):
#            output, states = decoder(embed, states)
#            outputs.append(embed)
#            fc = mx.sym.FullyConnected(data=output, weight=fc_weight, bias=fc_bias, num_hidden=len(targ_vocab), name='decoder_fc%d_'%i)
#            am = mx.sym.argmax(data=fc, axis=1)
#            embed = mx.sym.Embedding(data=am, weight=em_weight, input_dim=len(targ_vocab),
#                output_dim=args.num_embed, name='decoder_embed%d_'%i)

        # NEW 2
        for i in range(0, unroll_length):
            embed, states = decoder(embed, states)
            outputs.append(embed)

        outputs, _ = _normalize_sequence(unroll_length, outputs, layout, merge_outputs)

        return outputs, states

def train(args):

    from time import time

    data_train, data_val, src_vocab, targ_vocab = get_data('TN')
    print("len(src_vocab) len(targ_vocab)", len(src_vocab), len(targ_vocab))

    encoder = SequentialRNNCell()

    if args.use_cudnn_cells:
        encoder.add(mx.rnn.FusedRNNCell(args.num_hidden, num_layers=args.num_layers, dropout=args.dropout,
            mode='lstm', prefix='lstm_encoder_', bidirectional=args.bidirectional, get_next_state=True))
    else:
        for i in range(args.num_layers):
            if args.bidirectional:
                encoder.add(
                        mx.rnn.BidirectionalCell(
                            LSTMCell(args.num_hidden, prefix='lstm_encoder_l%d_'%i),
                            LSTMCell(args.num_hidden, prefix='lstm_encoder_r%d_'%i),
                            output_prefix='lstm_encoder_bi_l%d_'%i))
            else:
                encoder.add(LSTMCell(args.num_hidden, prefix='lstm_encoder_l%d_'%i))
            if i < args.num_layers - 1 and args.dropout > 0.0:
                encoder.add(mx.rnn.DropoutCell(args.dropout, prefix='lstm_encoder__dropout%d_' % i))
    encoder.add(AttentionEncoderCell())

    decoder = mx.rnn.SequentialRNNCell()

    if args.use_cudnn_cells:
        decoder.add(mx.rnn.FusedRNNCell(args.num_hidden, num_layers=args.num_layers,
            mode='lstm', prefix='lstm_decoder_', bidirectional=False, get_next_state=True))
    else:
        for i in range(args.num_layers):
            decoder.add(LSTMCell(args.num_hidden, prefix=('lstm_decoder_l%d_' % i)))
            if i < args.num_layers - 1 and args.dropout > 0.0:
                decoder.add(mx.rnn.DropoutCell(args.dropout, prefix='lstm_decoder_l%d_' % i))
    decoder.add(DotAttentionCell())

    def sym_gen(seq_len):
        src_data = mx.sym.Variable('src_data')
        targ_data = mx.sym.Variable('targ_data')
        label = mx.sym.Variable('softmax_label')

        src_embed = mx.sym.Embedding(data=src_data, input_dim=len(src_vocab),
                                 output_dim=args.num_embed, name='src_embed')
        targ_embed = mx.sym.Embedding(data=targ_data, input_dim=len(targ_vocab),    # data=data
                                 output_dim=args.num_embed, name='targ_embed')

        encoder.reset()
        decoder.reset()

        enc_seq_len, dec_seq_len = seq_len

        layout = 'TNC'
        _, states = encoder.unroll(enc_seq_len, inputs=src_embed, layout=layout)

        # This should be based on EOS or max seq len for inference, but here we unroll to the target length
        # TODO: fix <GO> symbol
        outputs, _ = decoder.unroll(dec_seq_len, targ_embed, begin_state=states, layout=layout, merge_outputs=True)
#        outputs, _ = decoder_unroll(decoder, targ_embed, targ_vocab, dec_seq_len, 0, begin_state=states, layout='TNC', merge_outputs=True)

        # NEW
        rs = mx.sym.Reshape(outputs, shape=(-1, args.num_hidden), name='sym_gen_reshape1')
        fc = mx.sym.FullyConnected(data=rs, num_hidden=len(targ_vocab), name='sym_gen_fc')
        label_rs = mx.sym.Reshape(data=label, shape=(-1,), name='sym_gen_reshape2')
        pred = mx.sym.SoftmaxOutput(data=fc, label=label_rs, name='sym_gen_softmax')

        return pred, ('src_data', 'targ_data',), ('softmax_label',)


#    foo, _, _ = sym_gen((1, 1))
#    print(type(foo))
#    mx.viz.plot_network(symbol=foo).save('./seq2seq.dot')


    if args.gpus:
        contexts = [mx.gpu(int(i)) for i in args.gpus.split(',')]
    else:
        contexts = mx.cpu(0)

    model = mx.mod.BucketingModule(
        sym_gen             = sym_gen,
        default_bucket_key  = data_train.default_bucket_key,
        context             = contexts)
    arg_params = None
    aux_params = None

    opt_params = {
      'learning_rate': args.lr,
      'wd': args.wd
    }

    if args.optimizer not in ['adadelta', 'adagrad', 'adam', 'rmsprop']:
        opt_params['momentum'] = args.mom

    opt_params['clip_gradient'] = args.max_grad_norm

    start = time()

    model.fit(
        train_data          = data_train,
        eval_data           = data_val,
        eval_metric         = mx.metric.Perplexity(invalid_label),
        kvstore             = args.kv_store,
        optimizer           = args.optimizer,
        optimizer_params    = opt_params,
        initializer         = mx.init.Xavier(factor_type="in", magnitude=2.34),
        arg_params          = arg_params,
        aux_params          = aux_params,
        begin_epoch         = args.load_epoch,
        num_epoch           = args.num_epochs,
        batch_end_callback  = mx.callback.Speedometer(batch_size=args.batch_size, frequent=args.disp_batches, auto_reset=True),
        epoch_end_callback  = mx.rnn.do_rnn_checkpoint(decoder, args.model_prefix, 1)
                              if args.model_prefix else None)

    train_duration = time() - start
    time_per_epoch = train_duration / args.num_epochs
    print("\n\nTime per epoch: %.2f seconds\n\n" % time_per_epoch)

class BleuScore(mx.metric.EvalMetric):
    def __init__(self, ignore_label, axis=-1):
        super(BleuScore, self).__init__('BleuScore')
        self.ignore_label = ignore_label
        self.axis = axis

    def update(self, labels, preds):
        assert len(labels) == len(preds)

        def drop_sentinels(text_lst):
            sentinels = lambda x: x == reserved_tokens['<PAD>'] or x == reserved_tokens['<GO>']
            text_lst = dropwhile(lambda x: sentinels(x), text_lst)
            text_lst = takewhile(lambda x: not sentinels(x) and x != reserved_tokens['<EOS>'], text_lst)
            return list(text_lst)

        smoothing_fn = nltk.translate.bleu_score.SmoothingFunction().method3 # method3

        for label, pred in zip(labels, preds):
            maxed = mx.ndarray.argmax(data=pred, axis=1)
            pred_nparr = maxed.asnumpy()
            label_nparr = label.asnumpy().astype(np.int32) 
            sent_len, batch_size = np.shape(label_nparr)
            pred_nparr = pred_nparr.reshape(sent_len, batch_size).astype(np.int32)

            for i in range(batch_size):
                exp_lst = drop_sentinels(label_nparr[:, i].tolist())
                act_lst = drop_sentinels(pred_nparr[:, i].tolist())
                expected = exp_lst
                actual = act_lst
                bleu = nltk.translate.bleu_score.sentence_bleu(
                    references=[expected], hypothesis=actual, weights=(0.25, 0.25, 0.25, 0.25),
                    smoothing_function = smoothing_fn 
                )
#                print("bleu: %f" % bleu)
                self.sum_metric += bleu
                self.num_inst += 1
            assert label.size == pred.size/pred.shape[-1], \
                "shape mismatch: %s vs. %s"%(label.shape, pred.shape)

    def get(self):
        num = self.num_inst if self.num_inst > 0 else float('nan')
        return (self.name, self.sum_metric/num)


def infer(args):
    assert args.model_prefix, "Must specifiy path to load from"

    data_train, data_val, src_vocab, targ_vocab = get_data('TN')

    print("len(src_vocab) len(targ_vocab)", len(src_vocab), len(targ_vocab))

    encoder = SequentialRNNCell()
    if args.use_cudnn_cells:
        encoder.add(mx.rnn.FusedRNNCell(args.num_hidden, num_layers=args.num_layers, dropout=args.dropout,
            mode='lstm', prefix='lstm_encoder_', bidirectional=args.bidirectional,
            get_next_state=True).unfuse())

    else:
        for i in range(args.num_layers):
            if args.bidirectional:
                encoder.add(
                        mx.rnn.BidirectionalCell(
                            LSTMCell(args.num_hidden, prefix='lstm_encoder_l%d_'%i),
                            LSTMCell(args.num_hidden, prefix='lstm_encoder_r%d_'%i),
                            output_prefix='lstm_encoder_bi_l%d_'%i))
            else:
                encoder.add(LSTMCell(args.num_hidden, prefix='lstm_encoder_l%d_'%i))
            if i < args.num_layers - 1 and args.dropout > 0.0:
                encoder.add(mx.rnn.DropoutCell(args.dropout, prefix='lstm_encoder__dropout%d_' % i))

    encoder.add(AttentionEncoderCell())

    decoder = mx.rnn.SequentialRNNCell()

    if args.use_cudnn_cells:
        decoder.add(mx.rnn.FusedRNNCell(args.num_hidden, num_layers=args.num_layers,
            mode='lstm', prefix='lstm_decoder_', bidirectional=False, get_next_state=True)).unfuse()
    else:
        for i in range(args.num_layers):
            decoder.add(LSTMCell(args.num_hidden, prefix=('lstm_decoder_l%d_' % i)))
            if i < args.num_layers - 1 and args.dropout > 0.0:
                decoder.add(mx.rnn.DropoutCell(args.dropout, prefix='lstm_decoder_l%d_' % i))
    decoder.add(DotAttentionCell())

    def sym_gen(seq_len):
        src_data = mx.sym.Variable('src_data')
        targ_data = mx.sym.Variable('targ_data')
        label = mx.sym.Variable('softmax_label')

        src_embed = mx.sym.Embedding(data=src_data, input_dim=len(src_vocab),
                                 output_dim=args.num_embed, name='src_embed')
        targ_embed = mx.sym.Embedding(data=targ_data, input_dim=len(targ_vocab),    # data=data
                                 output_dim=args.num_embed, name='targ_embed')

        encoder.reset()
        decoder.reset()

        enc_seq_len, dec_seq_len = seq_len

        layout = 'TNC'
        _, states = encoder.unroll(enc_seq_len, inputs=src_embed, layout=layout)

        # This should be based on EOS or max seq len for inference, but here we unroll to the target length
        # TODO: fix <GO> symbol
#        outputs, _ = decoder.unroll(dec_seq_len, targ_embed, begin_state=states, layout=layout, merge_outputs=True)
        outputs, _ = decoder_unroll(decoder, targ_embed, targ_vocab, dec_seq_len, 0, begin_state=states, layout='TNC', merge_outputs=True)

        # NEW
        rs = mx.sym.Reshape(outputs, shape=(-1, args.num_hidden), name='sym_gen_reshape1')
        fc = mx.sym.FullyConnected(data=rs, num_hidden=len(targ_vocab), name='sym_gen_fc')
        label_rs = mx.sym.Reshape(data=label, shape=(-1,), name='sym_gen_reshape2')
        pred = mx.sym.SoftmaxOutput(data=fc, label=label_rs, name='sym_gen_softmax')

        return pred, ('src_data', 'targ_data',), ('softmax_label',)

    if args.gpus:
        contexts = [mx.gpu(int(i)) for i in args.gpus.split(',')]
    else:
        contexts = mx.cpu(0)

    model = mx.mod.BucketingModule(
        sym_gen             = sym_gen,
        default_bucket_key  = data_train.default_bucket_key,
        context             = contexts)

    model.bind(data_val.provide_data, data_val.provide_label, for_training=False)

    if args.load_epoch:
        _, arg_params, aux_params = mx.rnn.load_rnn_checkpoint(
            decoder, args.model_prefix, args.load_epoch)
        model.set_params(arg_params, aux_params)

    else:
        arg_params = None
        aux_params = None

    opt_params = {
      'learning_rate': args.lr,
      'wd': args.wd
    }

    if args.optimizer not in ['adadelta', 'adagrad', 'adam', 'rmsprop']:
        opt_params['momentum'] = args.mom

    opt_params['clip_gradient'] = args.max_grad_norm

    start = time()

    # mx.metric.Perplexity
    model.score(data_val, BleuScore(invalid_label), #PPL(invalid_label),
                batch_end_callback=mx.callback.Speedometer(batch_size=args.batch_size, frequent=5, auto_reset=True))

    infer_duration = time() - start
    time_per_epoch = infer_duration / args.num_epochs
    print("\n\nTime per epoch: %.2f seconds\n\n" % time_per_epoch)

if __name__ == '__main__':
    import logging
    head = '%(asctime)-15s %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=head)

    args = parser.parse_args()
    if args.gpus:
        contexts = [mx.gpu(int(i)) for i in args.gpus.split(',')]
    else:
        contexts = mx.cpu(0)


    if args.num_layers >= 4 and len(args.gpus.split(',')) >= 4 and not args.stack_rnn:
        print('WARNING: stack-rnn is recommended to train complex model on multiple GPUs')

    if args.infer:
        # Demonstrates how to load a model trained with CuDNN RNN and predict
        # with non-fused MXNet symbol
        infer(args)
    else:
        train(args)
